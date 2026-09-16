"""Open Terminal Operator — Kopf handlers for the Terminal CRD.

Watches ``Terminal`` custom resources (``terminals.openwebui.com/v1alpha1``)
and reconciles the underlying Kubernetes resources:

- **Secret** holding a generated API key
- **Pod** running the open-terminal container
- **Service** (ClusterIP) exposing port 8000
- **PVC** (optional) for persistent ``/home/user`` storage

The orchestrator creates/deletes Terminal CRs; this operator does the rest.

Ported from the ``kubernetes-controller`` branch with the ABC-compatible
``openwebui.com`` API group retained for extensibility.
"""

import base64
import json
import logging
import os
import re
import secrets
import string
from datetime import datetime, timezone

import kopf
import kubernetes
from kubernetes import client as k8s

log = logging.getLogger(__name__)

_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

GROUP = "openwebui.com"
VERSION = "v1alpha1"
PLURAL = "terminals"


def _reconcile_interval() -> float:
    """Seconds between reconcile sweeps that re-create missing children."""
    raw = os.environ.get("TERMINALS_RECONCILE_INTERVAL_SECONDS", "15")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("Invalid TERMINALS_RECONCILE_INTERVAL_SECONDS=%r; using 15", raw)
        return 15.0
    return value if value > 0 else 15.0


RECONCILE_INTERVAL = _reconcile_interval()

# Pod phases that mean "this pod is still doing its job".  Anything else
# (Succeeded, Failed, Unknown) is replaced by the reconciler.
LIVE_POD_PHASES = ("Pending", "Running")

RESTRICTED_POD_SECURITY_CONTEXT = {
    "runAsNonRoot": True,
    "seccompProfile": {"type": "RuntimeDefault"},
}
RESTRICTED_CONTAINER_SECURITY_CONTEXT = {
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
    "runAsNonRoot": True,
}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def _configured_log_level() -> int:
    raw = os.environ.get("TERMINALS_LOG_LEVEL", "INFO")
    level = raw.strip().upper()
    if level in _LOG_LEVELS:
        return _LOG_LEVELS[level]

    log.warning(
        "Invalid TERMINALS_LOG_LEVEL=%r; using INFO. Expected one of: %s",
        raw,
        ", ".join(_LOG_LEVELS),
    )
    return logging.INFO


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    """Load K8s config and configure kopf settings."""
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()
    log_level = _configured_log_level()
    logging.getLogger().setLevel(log_level)
    settings.posting.level = max(log_level, logging.WARNING)
    settings.persistence.finalizer = "terminals.openwebui.com/finalizer"
    _load_scheduling_config()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_api_key(length: int = 48) -> str:
    alphabet = string.ascii_letters + string.digits
    return "sk-" + "".join(secrets.choice(alphabet) for _ in range(length))


def _resource_name(name: str, suffix: str) -> str:
    """Derive child resource names from the Terminal CR name."""
    return f"{name}-{suffix}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(Ki|Mi|Gi|Ti)?$")
_CPU_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(m)?$")
_SIZE_MULT = {"": 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}


def _parse_size(value: str) -> int:
    m = _SIZE_RE.match(str(value).strip())
    if not m:
        return int(value)
    num, suffix = float(m.group(1)), m.group(2) or ""
    return int(num * _SIZE_MULT[suffix])


def _parse_cpu_nanos(value: str) -> int:
    m = _CPU_RE.match(str(value).strip())
    if not m:
        return int(float(value) * 1_000_000_000)
    num, suffix = float(m.group(1)), m.group(2) or ""
    if suffix == "m":
        return int(num * 1_000_000)
    return int(num * 1_000_000_000)


def _format_cpu_count(value: str) -> str:
    cores = _parse_cpu_nanos(value) / 1_000_000_000
    if cores.is_integer():
        return str(int(cores))
    return f"{cores:.3f}".rstrip("0").rstrip(".")


def _deep_merge(*items: dict | None) -> dict:
    result = {}
    for item in items:
        if not item:
            continue
        for key, value in item.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = _deep_merge(result[key], value)
            else:
                result[key] = value
    return result


def _merge_by_name(base: list[dict] | None, override: list[dict] | None) -> list[dict]:
    result = [dict(item) for item in base or []]
    indexes = {
        item.get("name"): index
        for index, item in enumerate(result)
        if isinstance(item, dict) and item.get("name")
    }
    for item in override or []:
        if not isinstance(item, dict):
            result.append(item)
            continue
        name = item.get("name")
        if name and name in indexes:
            result[indexes[name]] = _deep_merge(result[indexes[name]], item)
        else:
            if name:
                indexes[name] = len(result)
            result.append(dict(item))
    return result


def _apply_pod_template(pod_manifest: dict, pod_template: dict | None) -> dict:
    """Merge user-supplied PodTemplate-style overrides into the generated pod.

    The template can add pod spec fields, sidecars, volumes, mounts, labels,
    and annotations. Generated identity, owner references, and the required
    open-terminal auth/port wiring remain authoritative.
    """
    if not isinstance(pod_template, dict):
        return pod_manifest

    template_metadata = pod_template.get("metadata") or {}
    if isinstance(template_metadata, dict):
        metadata = pod_manifest["metadata"]
        merged_metadata = _deep_merge(template_metadata, metadata)
        merged_metadata["labels"] = _deep_merge(
            template_metadata.get("labels"), metadata.get("labels")
        )
        if template_metadata.get("annotations") or metadata.get("annotations"):
            merged_metadata["annotations"] = _deep_merge(
                template_metadata.get("annotations"), metadata.get("annotations")
            )
        merged_metadata["name"] = metadata["name"]
        merged_metadata["namespace"] = metadata["namespace"]
        merged_metadata["ownerReferences"] = metadata["ownerReferences"]
        pod_manifest["metadata"] = merged_metadata

    template_spec = pod_template.get("spec") or {}
    if isinstance(template_spec, dict):
        generated_spec = pod_manifest["spec"]
        merged_spec = _deep_merge(template_spec, generated_spec)
        containers = _merge_by_name(
            template_spec.get("containers"), generated_spec.get("containers")
        )
        for container in containers:
            if container.get("name") == "open-terminal":
                template_container = next(
                    (
                        item
                        for item in template_spec.get("containers") or []
                        if item.get("name") == "open-terminal"
                    ),
                    {},
                )
                generated_container = generated_spec["containers"][0]
                for field in ("env", "ports", "volumeMounts"):
                    container[field] = _merge_by_name(
                        template_container.get(field), generated_container.get(field)
                    )
                break
        merged_spec["containers"] = containers
        if template_spec.get("volumes") or generated_spec.get("volumes"):
            merged_spec["volumes"] = _merge_by_name(
                template_spec.get("volumes"), generated_spec.get("volumes")
            )
        pod_manifest["spec"] = merged_spec

    return pod_manifest


def _resource_limit_env(resources_spec: dict) -> list[dict]:
    limits = resources_spec.get("limits", {})
    env = []

    cpu_limit = limits.get("cpu")
    if cpu_limit:
        cpu_limit = str(cpu_limit)
        env.append({"name": "OPEN_TERMINAL_CPU_LIMIT", "value": cpu_limit})
        env.append(
            {"name": "OPEN_TERMINAL_CPU_COUNT", "value": _format_cpu_count(cpu_limit)}
        )

    memory_limit = limits.get("memory")
    if memory_limit:
        memory_limit = str(memory_limit)
        env.append({"name": "OPEN_TERMINAL_MEMORY_LIMIT", "value": memory_limit})
        env.append(
            {"name": "OPEN_TERMINAL_MEMORY_BYTES", "value": str(_parse_size(memory_limit))}
        )

    return env


def _set_env_var(env: list[dict], name: str, value: str) -> None:
    for item in env:
        if item.get("name") == name:
            item["value"] = value
            return
    env.append({"name": name, "value": value})


def _owner_ref(body: dict) -> dict:
    """Build a single ownerReference dict for garbage collection."""
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "Terminal",
        "name": body["metadata"]["name"],
        "uid": body["metadata"]["uid"],
        "controller": True,
        "blockOwnerDeletion": True,
    }


def _labels(name: str, user_id: str = "") -> dict[str, str]:
    labels = {
        "app.kubernetes.io/name": "open-terminal",
        "app.kubernetes.io/instance": name,
        "app.kubernetes.io/managed-by": "terminals",
        "app.kubernetes.io/part-of": "open-terminal",
        "openwebui.com/terminal": name,
    }
    if user_id:
        labels["openwebui.com/user-id"] = user_id
    return labels


def _set_condition(
    status: dict,
    cond_type: str,
    cond_status: str,
    reason: str = "",
    message: str = "",
) -> list:
    """Create or update a condition in the conditions list."""
    conditions = list(status.get("conditions") or [])
    for c in conditions:
        if c["type"] == cond_type:
            c["status"] = cond_status
            c["lastTransitionTime"] = _now_iso()
            c["reason"] = reason
            c["message"] = message
            return conditions
    conditions.append(
        {
            "type": cond_type,
            "status": cond_status,
            "lastTransitionTime": _now_iso(),
            "reason": reason,
            "message": message,
        }
    )
    return conditions


def _patch_terminal_status(namespace: str, terminal_name: str, status: dict) -> None:
    """Merge-patch the Terminal's status subresource.

    ``_content_type`` is explicit: the client otherwise picks the first
    accepted type (``application/json-patch+json``), which rejects a dict body.
    """
    custom_api = k8s.CustomObjectsApi()
    try:
        custom_api.patch_namespaced_custom_object_status(
            group=GROUP,
            version=VERSION,
            namespace=namespace,
            plural=PLURAL,
            name=terminal_name,
            body={"status": status},
            _content_type="application/merge-patch+json",
        )
    except k8s.exceptions.ApiException as e:
        if e.status == 404:
            return
        log.warning(
            "Failed to patch Terminal %s/%s status: %s", namespace, terminal_name, e
        )


def _parse_node_selector() -> dict[str, str] | None:
    raw = os.environ.get("TERMINALS_KUBERNETES_NODE_SELECTOR", "").strip()
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"TERMINALS_KUBERNETES_NODE_SELECTOR is not valid JSON ({e}); "
                "expected a single JSON object"
            ) from e
        if not isinstance(data, dict):
            raise ValueError("TERMINALS_KUBERNETES_NODE_SELECTOR must be an object")
        return {str(key): str(value) for key, value in data.items()}

    selector = {}
    for pair in raw.split(","):
        if "=" not in pair:
            raise ValueError(
                "TERMINALS_KUBERNETES_NODE_SELECTOR must be JSON or k=v pairs"
            )
        key, value = pair.split("=", 1)
        selector[key.strip()] = value.strip()
    return selector or None


def _parse_tolerations() -> list[dict] | None:
    raw = os.environ.get("TERMINALS_KUBERNETES_TOLERATIONS", "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"TERMINALS_KUBERNETES_TOLERATIONS is not valid JSON ({e}); "
            "expected a single JSON array of toleration objects"
        ) from e
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError("TERMINALS_KUBERNETES_TOLERATIONS must be a JSON array")
    return data


# Scheduling config comes from the operator's own environment, so it is
# constant for the life of the process.  It is parsed once at startup rather
# than on every pod build: a malformed value then stops the operator
# immediately with a clear error, instead of being re-raised out of every
# reconcile sweep forever while no pod is ever created.
NODE_SELECTOR: dict[str, str] | None = None
TOLERATIONS: list[dict] | None = None


def _load_scheduling_config() -> None:
    """Parse and cache the scheduling env vars.  Raises on malformed input."""
    global NODE_SELECTOR, TOLERATIONS
    NODE_SELECTOR = _parse_node_selector()
    TOLERATIONS = _parse_tolerations()


# ---------------------------------------------------------------------------
# Manifest builders
# ---------------------------------------------------------------------------


def _build_pod_manifest(
    name: str,
    namespace: str,
    spec: dict,
    api_key: str,
    owner_ref: dict,
    pvc_name: str | None,
    user_id: str = "",
) -> dict:
    """Build the Pod manifest for an Open Terminal instance."""
    image = spec.get("image", "ghcr.io/open-webui/open-terminal:latest")
    resources_spec = spec.get("resources", {})
    packages = spec.get("packages", [])
    pip_packages = spec.get("pipPackages", [])

    env = [
        {"name": "OPEN_TERMINAL_API_KEY", "value": api_key},
        {"name": "OPEN_TERMINAL_HOST", "value": "0.0.0.0"},
        {"name": "OPEN_TERMINAL_PORT", "value": "8000"},
    ]
    if packages:
        env.append({"name": "OPEN_TERMINAL_PACKAGES", "value": " ".join(packages)})
    if pip_packages:
        env.append(
            {"name": "OPEN_TERMINAL_PIP_PACKAGES", "value": " ".join(pip_packages)}
        )
    for key, value in spec.get("env", {}).items():
        key = str(key)
        if key != "OPEN_TERMINAL_API_KEY" and value is not None:
            _set_env_var(env, key, str(value))
    for item in _resource_limit_env(resources_spec):
        _set_env_var(env, item["name"], item["value"])

    volume_mounts = []
    volumes = []
    if pvc_name:
        volume_mounts.append({"name": "home", "mountPath": "/home/user"})
        volumes.append(
            {"name": "home", "persistentVolumeClaim": {"claimName": pvc_name}}
        )

    container = {
        "name": "open-terminal",
        "image": image,
        "ports": [{"containerPort": 8000, "name": "http", "protocol": "TCP"}],
        "env": env,
        "volumeMounts": volume_mounts,
        "readinessProbe": {
            "httpGet": {"path": "/health", "port": 8000},
            "initialDelaySeconds": 3,
            "periodSeconds": 5,
        },
        "livenessProbe": {
            "httpGet": {"path": "/health", "port": 8000},
            "initialDelaySeconds": 10,
            "periodSeconds": 15,
        },
    }

    requests = resources_spec.get("requests", {})
    limits = resources_spec.get("limits", {})
    if requests or limits:
        container["resources"] = {}
        if requests:
            container["resources"]["requests"] = requests
        if limits:
            container["resources"]["limits"] = limits

    restricted = bool(spec.get("restricted"))
    container_security_context = _deep_merge(
        RESTRICTED_CONTAINER_SECURITY_CONTEXT if restricted else {},
        spec.get("containerSecurityContext"),
    )
    if container_security_context:
        container["securityContext"] = container_security_context

    pod_labels = _labels(name, user_id)
    pod_spec = {
        "containers": [container],
        "volumes": volumes,
        "restartPolicy": "Always",
        "enableServiceLinks": False,
        "automountServiceAccountToken": False,
    }
    if NODE_SELECTOR:
        pod_spec["nodeSelector"] = NODE_SELECTOR
    if TOLERATIONS:
        pod_spec["tolerations"] = TOLERATIONS
    pod_security_context = _deep_merge(
        RESTRICTED_POD_SECURITY_CONTEXT if restricted else {},
        spec.get("podSecurityContext"),
    )
    if pod_security_context:
        pod_spec["securityContext"] = pod_security_context

    pod_manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": _resource_name(name, "pod"),
            "namespace": namespace,
            "labels": pod_labels,
            "ownerReferences": [owner_ref],
        },
        "spec": pod_spec,
    }
    return _apply_pod_template(pod_manifest, spec.get("podTemplate"))


def _build_service_manifest(
    name: str, namespace: str, owner_ref: dict, user_id: str = ""
) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": _resource_name(name, "svc"),
            "namespace": namespace,
            "labels": _labels(name, user_id),
            "ownerReferences": [owner_ref],
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {"openwebui.com/terminal": name},
            "ports": [
                {"name": "http", "port": 8000, "targetPort": 8000, "protocol": "TCP"}
            ],
        },
    }


def _build_secret_manifest(
    name: str, namespace: str, api_key: str, owner_ref: dict,
    user_id: str = "",
) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": _resource_name(name, "apikey"),
            "namespace": namespace,
            "labels": _labels(name, user_id),
            "ownerReferences": [owner_ref],
        },
        "type": "Opaque",
        "data": {
            "api-key": base64.b64encode(api_key.encode()).decode(),
        },
    }


def _build_pvc_manifest(
    name: str, namespace: str, spec: dict, owner_ref: dict,
    user_id: str = "",
) -> dict:
    persistence = spec.get("persistence", {})
    size = persistence.get("size", "1Gi")
    storage_class = persistence.get("storageClass", "")

    # NOTE: PVCs intentionally have NO ownerReference so they survive
    # Terminal CR deletion.  User workspace data must persist across
    # idle cleanup and refresh cycles.
    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": _resource_name(name, "pvc"),
            "namespace": namespace,
            "labels": _labels(name, user_id),
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": size}},
        },
    }
    if storage_class:
        pvc["spec"]["storageClassName"] = storage_class
    return pvc


# ---------------------------------------------------------------------------
# Child resource reconciliation
# ---------------------------------------------------------------------------


def _ensure_pvc(core_v1, name, namespace, spec, owner_ref, user_id) -> None:
    pvc_name = _resource_name(name, "pvc")
    manifest = _build_pvc_manifest(name, namespace, spec, owner_ref, user_id=user_id)
    try:
        core_v1.create_namespaced_persistent_volume_claim(
            namespace=namespace, body=manifest
        )
        log.info("Created PVC %s/%s", namespace, pvc_name)
    except k8s.exceptions.ApiException as e:
        if e.status != 409:
            raise


def _ensure_secret(core_v1, name, namespace, owner_ref, user_id) -> str:
    """Return the terminal API key, creating the Secret if it is missing.

    The key is read back from an existing Secret so that a re-created Pod
    keeps the credential the orchestrator already handed out.
    """
    secret_name = _resource_name(name, "apikey")
    try:
        existing = core_v1.read_namespaced_secret(secret_name, namespace)
        raw = (existing.data or {}).get("api-key")
        if raw:
            return base64.b64decode(raw).decode()
        # Secret exists but is empty — repopulate it rather than 409-looping.
        api_key = _generate_api_key()
        core_v1.patch_namespaced_secret(
            secret_name,
            namespace,
            {"data": {"api-key": base64.b64encode(api_key.encode()).decode()}},
        )
        log.warning("Secret %s/%s had no api-key; regenerated", namespace, secret_name)
        return api_key
    except k8s.exceptions.ApiException as e:
        if e.status != 404:
            raise

    api_key = _generate_api_key()
    manifest = _build_secret_manifest(
        name, namespace, api_key, owner_ref, user_id=user_id
    )
    try:
        core_v1.create_namespaced_secret(namespace=namespace, body=manifest)
        log.info("Created Secret %s/%s", namespace, secret_name)
        return api_key
    except k8s.exceptions.ApiException as e:
        if e.status != 409:
            raise
    existing = core_v1.read_namespaced_secret(secret_name, namespace)
    return base64.b64decode((existing.data or {})["api-key"]).decode()


def _ensure_service(core_v1, name, namespace, owner_ref, user_id) -> None:
    svc_name = _resource_name(name, "svc")
    try:
        core_v1.read_namespaced_service(svc_name, namespace)
        return
    except k8s.exceptions.ApiException as e:
        if e.status != 404:
            raise

    manifest = _build_service_manifest(name, namespace, owner_ref, user_id=user_id)
    try:
        core_v1.create_namespaced_service(namespace=namespace, body=manifest)
        log.info("Created Service %s/%s", namespace, svc_name)
    except k8s.exceptions.ApiException as e:
        if e.status != 409:
            raise


def _pod_state(core_v1, pod_name: str, namespace: str) -> str:
    """Classify the terminal Pod as ``live``, ``terminating``, ``dead`` or ``gone``."""
    try:
        pod = core_v1.read_namespaced_pod(pod_name, namespace)
    except k8s.exceptions.ApiException as e:
        if e.status == 404:
            return "gone"
        raise

    if (pod.metadata.deletion_timestamp if pod.metadata else None) is not None:
        return "terminating"
    phase = (pod.status.phase if pod.status else None) or "Unknown"
    return "live" if phase in LIVE_POD_PHASES else "dead"


def _live_phase(namespace: str, name: str) -> str | None:
    """Read the Terminal's phase straight from the API server (no cache)."""
    custom_api = k8s.CustomObjectsApi()
    try:
        cr = custom_api.get_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=namespace,
            plural=PLURAL,
            name=name,
        )
    except k8s.exceptions.ApiException:
        return None
    return (cr.get("status") or {}).get("phase")


def _ensure_children(
    body: dict,
    spec: dict,
    name: str,
    namespace: str,
    phase_guard=None,
) -> dict:
    """Create any missing child resource for a Terminal CR.

    Safe to call repeatedly — this is the single code path used by the create
    handler, the reconcile timer, and the pod-deleted watcher.  Returns the
    resolved child names plus ``pod_created`` so callers know whether the Pod
    was just (re)created and the status needs to go back to ``Pending``.

    *phase_guard* is an optional callable re-read just before the Pod would be
    touched; if it reports ``Idle`` the repair is abandoned, so a reconcile
    running against a stale cache can't undo an idle cull.
    """
    user_id = spec.get("userId", "")
    owner_ref = _owner_ref(body)
    core_v1 = k8s.CoreV1Api()

    pod_name = _resource_name(name, "pod")
    svc_name = _resource_name(name, "svc")
    secret_name = _resource_name(name, "apikey")

    _ensure_service(core_v1, name, namespace, owner_ref, user_id)

    state = _pod_state(core_v1, pod_name, namespace)
    pod_created = False

    if state != "live" and phase_guard is not None and phase_guard() == "Idle":
        log.debug("Terminal %s/%s went idle; skipping pod repair", namespace, name)
        state = "idle"

    if state == "dead":
        # Succeeded/Failed pods never come back on their own — clear the way
        # and let the next pass create a fresh one.
        log.info("Pod %s/%s is not alive; deleting for replacement", namespace, pod_name)
        try:
            core_v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
        except k8s.exceptions.ApiException as e:
            if e.status != 404:
                raise
        state = "terminating"

    if state == "gone":
        persistence = spec.get("persistence", {})
        pvc_name = None
        if persistence.get("enabled", True):
            pvc_name = _resource_name(name, "pvc")
            _ensure_pvc(core_v1, name, namespace, spec, owner_ref, user_id)

        api_key = _ensure_secret(core_v1, name, namespace, owner_ref, user_id)
        manifest = _build_pod_manifest(
            name, namespace, spec, api_key, owner_ref, pvc_name, user_id=user_id
        )
        try:
            core_v1.create_namespaced_pod(namespace=namespace, body=manifest)
            log.info("Created Pod %s/%s", namespace, pod_name)
            pod_created = True
        except k8s.exceptions.ApiException as e:
            if e.status != 409:
                raise
            log.info("Pod %s/%s already exists", namespace, pod_name)

    return {
        "pod_name": pod_name,
        "service_name": svc_name,
        "secret_name": secret_name,
        "service_url": f"http://{svc_name}.{namespace}.svc:8000",
        "pod_created": pod_created,
        "pod_state": state,
    }


# ---------------------------------------------------------------------------
# Create handler
# ---------------------------------------------------------------------------


@kopf.on.create(GROUP, VERSION, PLURAL)
async def on_create(body, spec, name, namespace, patch, **_):
    """Create all child resources for a new Terminal CR."""
    log.info("Creating terminal %s/%s for user %s", namespace, name, spec.get("userId"))

    result = _ensure_children(body, spec, name, namespace)

    patch.status["podName"] = result["pod_name"]
    patch.status["serviceName"] = result["service_name"]
    patch.status["serviceUrl"] = result["service_url"]
    patch.status["apiKeySecret"] = result["secret_name"]
    patch.status["lastActivityAt"] = _now_iso()
    patch.status["phase"] = "Pending"
    patch.status["conditions"] = _set_condition(
        {},
        "Ready",
        "False",
        "PodNotReady",
        "Waiting for pod to become ready",
    )


# ---------------------------------------------------------------------------
# Reconcile — re-create children that disappeared out from under us
# ---------------------------------------------------------------------------


def _reconcile(body, spec, status, meta, name, namespace, trigger: str) -> None:
    """Level-triggered repair of a Terminal's child resources.

    ``on.create`` only fires once, so a Pod deleted afterwards (manual
    ``kubectl delete``, eviction, node loss, operator downtime) used to stay
    gone while the CR still advertised ``phase: Running`` — the orchestrator
    then kept proxying to a Service with no endpoints.
    """
    if meta.get("deletionTimestamp"):
        return

    phase = (status or {}).get("phase")
    if phase == "Idle":
        # Idle culling deletes the pod on purpose; ``idle_check`` owns that
        # state and the orchestrator re-creates the CR on next access.
        return

    result = _ensure_children(
        body,
        spec,
        name,
        namespace,
        phase_guard=lambda: _live_phase(namespace, name),
    )
    if result["pod_state"] == "idle":
        return

    current = status or {}
    updates: dict = {}
    for field, value in (
        ("podName", result["pod_name"]),
        ("serviceName", result["service_name"]),
        ("serviceUrl", result["service_url"]),
        ("apiKeySecret", result["secret_name"]),
    ):
        if current.get(field) != value:
            updates[field] = value

    if result["pod_created"]:
        log.info(
            "Re-created missing pod for terminal %s/%s (%s)", namespace, name, trigger
        )
        updates["phase"] = "Pending"
        updates["conditions"] = _set_condition(
            current, "Ready", "False", "PodRecreated", "Re-creating missing pod"
        )
    elif result["pod_state"] != "live" and phase == "Running":
        # Pod is on its way out — stop advertising the terminal as usable.
        updates["phase"] = "Pending"
        updates["conditions"] = _set_condition(
            current, "Ready", "False", "PodNotReady", "Pod is terminating"
        )

    if updates:
        _patch_terminal_status(namespace, name, updates)


@kopf.timer(GROUP, VERSION, PLURAL, interval=RECONCILE_INTERVAL)
def reconcile_timer(body, spec, status, meta, name, namespace, **_):
    """Periodically ensure every non-idle Terminal still has its children."""
    _reconcile(body, spec, status, meta, name, namespace, "timer")


@kopf.on.resume(GROUP, VERSION, PLURAL)
def on_resume(body, spec, status, meta, name, namespace, **_):
    """Re-adopt existing Terminals when the operator restarts."""
    _reconcile(body, spec, status, meta, name, namespace, "resume")


# ---------------------------------------------------------------------------
# Delete handler (cleanup is automatic via ownerReferences, but log it)
# ---------------------------------------------------------------------------


@kopf.on.delete(GROUP, VERSION, PLURAL)
async def on_delete(name, namespace, **_):
    """Log deletion — child resources are cleaned up via ownerReferences."""
    log.info(
        "Terminal %s/%s deleted. Child resources will be garbage-collected.",
        namespace,
        name,
    )


# ---------------------------------------------------------------------------
# Pod watcher — update Terminal status when pod phase changes
# ---------------------------------------------------------------------------


def _on_pod_lost(terminal: dict, namespace: str, terminal_name: str) -> None:
    """A terminal Pod vanished — flip the CR out of Running and re-create it.

    Status is patched *before* the pod is re-created so the orchestrator stops
    routing to the dead endpoint immediately instead of waiting for a proxy
    connection error to time out.
    """
    current_status = terminal.get("status") or {}
    log.info("Pod for terminal %s/%s was deleted; re-creating", namespace, terminal_name)
    _patch_terminal_status(
        namespace,
        terminal_name,
        {
            "phase": "Pending",
            "conditions": _set_condition(
                current_status, "Ready", "False", "PodDeleted", "Pod was deleted"
            ),
        },
    )
    try:
        _ensure_children(
            terminal, terminal.get("spec") or {}, terminal_name, namespace
        )
    except k8s.exceptions.ApiException as e:
        # The reconcile timer retries; don't let a transient API error kill
        # the pod watcher.
        log.warning(
            "Could not re-create pod for terminal %s/%s: %s", namespace, terminal_name, e
        )


@kopf.on.event("v1", "pods", labels={"app.kubernetes.io/managed-by": "terminals"})
async def on_pod_event(event, body, **_):
    """Watch terminal pods and reflect readiness back into the Terminal CR status."""
    pod = body
    labels = pod.get("metadata", {}).get("labels", {})
    terminal_name = labels.get("openwebui.com/terminal")
    if not terminal_name:
        return

    namespace = pod["metadata"]["namespace"]
    pod_phase = (pod.get("status") or {}).get("phase", "Unknown")

    # Check container readiness
    container_statuses = (pod.get("status") or {}).get("containerStatuses", [])
    is_ready = any(cs.get("ready", False) for cs in container_statuses)

    custom_api = k8s.CustomObjectsApi()
    try:
        terminal = custom_api.get_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=namespace,
            plural=PLURAL,
            name=terminal_name,
        )
    except k8s.exceptions.ApiException as e:
        if e.status == 404:
            return
        raise

    current_status = terminal.get("status", {})
    current_phase = current_status.get("phase")

    # Don't update if terminal is being torn down
    if current_phase in ("Idle",):
        return
    if terminal.get("metadata", {}).get("deletionTimestamp"):
        return

    if event.get("type") == "DELETED":
        _on_pod_lost(terminal, namespace, terminal_name)
        return

    # A pod with a deletionTimestamp still reports Running/ready for its whole
    # grace period — don't keep advertising it as usable.
    terminating = pod.get("metadata", {}).get("deletionTimestamp") is not None
    if terminating:
        is_ready = False

    new_phase = current_phase
    if is_ready and pod_phase == "Running":
        new_phase = "Running"
    elif terminating or pod_phase in ("Pending",):
        new_phase = "Pending"
    elif pod_phase in ("Failed", "Unknown"):
        new_phase = "Error"

    if new_phase == current_phase and current_phase == "Running" and is_ready:
        return  # No change needed

    conditions = _set_condition(
        current_status,
        "Ready",
        "True" if is_ready else "False",
        "PodReady" if is_ready else "PodNotReady",
        "Pod is terminating" if terminating else f"Pod phase: {pod_phase}",
    )

    status_patch = {"phase": new_phase, "conditions": conditions}
    if is_ready and new_phase == "Running":
        status_patch["lastActivityAt"] = _now_iso()

    _patch_terminal_status(namespace, terminal_name, status_patch)


# ---------------------------------------------------------------------------
# Idle timeout timer
# ---------------------------------------------------------------------------


@kopf.timer(GROUP, VERSION, PLURAL, interval=60, idle=60)
async def idle_check(spec, status, name, namespace, **_):
    """Periodically check if a terminal has exceeded its idle timeout."""
    phase = (status or {}).get("phase")
    if phase not in ("Running", "Idle"):
        return

    last_activity = (status or {}).get("lastActivityAt")
    if not last_activity:
        return

    timeout_minutes = spec.get("idleTimeoutMinutes", 30)
    try:
        last_dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return

    elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60

    if elapsed < timeout_minutes:
        return

    log.info(
        "Terminal %s/%s idle for %.1f min (timeout=%d). Deleting pod.",
        namespace,
        name,
        elapsed,
        timeout_minutes,
    )

    pod_name = (status or {}).get("podName")
    if not pod_name:
        return

    # Mark Idle *before* deleting so the pod watcher and the reconcile timer
    # see the intent and don't treat the deletion as a pod loss to repair.
    _patch_terminal_status(
        namespace,
        name,
        {
            "phase": "Idle",
            "conditions": _set_condition(
                status,
                "Ready",
                "False",
                "IdleTimeout",
                f"Pod deleted after {elapsed:.0f} min of inactivity",
            ),
        },
    )

    # Delete the pod to free resources; the PVC, Secret, and CRD remain
    core_v1 = k8s.CoreV1Api()
    try:
        core_v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
    except k8s.exceptions.ApiException as e:
        if e.status == 404:
            log.info("Pod %s/%s already gone", namespace, pod_name)
        else:
            raise
