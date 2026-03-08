"""Open Terminal Operator — Kopf handlers for the Terminal CRD.

Watches ``Terminal`` custom resources (``terminals.openwebui.com/v1alpha1``)
and reconciles the underlying Kubernetes resources:

- **Secret** holding a generated API key
- **Pod** running the open-terminal container
- **Service** (ClusterIP) exposing port 8000
- **PVC** (optional) for persistent ``/workspace`` storage

The orchestrator creates/deletes Terminal CRs; this operator does the rest.

Ported from the ``kubernetes-controller`` branch with the ABC-compatible
``openwebui.com`` API group retained for extensibility.
"""

import base64
import logging
import secrets
import string
from datetime import datetime, timezone

import kopf
import kubernetes
from kubernetes import client as k8s

log = logging.getLogger(__name__)

GROUP = "openwebui.com"
VERSION = "v1alpha1"
PLURAL = "terminals"


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    """Load K8s config and configure kopf settings."""
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()
    settings.posting.level = logging.WARNING
    settings.persistence.finalizer = "terminals.openwebui.com/finalizer"


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


def _labels(name: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": "open-terminal",
        "app.kubernetes.io/instance": name,
        "app.kubernetes.io/managed-by": "open-terminal-operator",
        "openwebui.com/terminal": name,
    }


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
        env.append({"name": "OPEN_TERMINAL_PIP_PACKAGES", "value": " ".join(pip_packages)})

    volume_mounts = []
    volumes = []
    if pvc_name:
        volume_mounts.append({"name": "workspace", "mountPath": "/workspace"})
        volumes.append(
            {"name": "workspace", "persistentVolumeClaim": {"claimName": pvc_name}}
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

    pod_labels = _labels(name)

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": _resource_name(name, "pod"),
            "namespace": namespace,
            "labels": pod_labels,
            "ownerReferences": [owner_ref],
        },
        "spec": {
            "containers": [container],
            "volumes": volumes,
            "restartPolicy": "Always",
            "enableServiceLinks": False,
            "automountServiceAccountToken": False,
        },
    }


def _build_service_manifest(
    name: str, namespace: str, owner_ref: dict
) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": _resource_name(name, "svc"),
            "namespace": namespace,
            "labels": _labels(name),
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
    name: str, namespace: str, api_key: str, owner_ref: dict
) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": _resource_name(name, "apikey"),
            "namespace": namespace,
            "labels": _labels(name),
            "ownerReferences": [owner_ref],
        },
        "type": "Opaque",
        "data": {
            "api-key": base64.b64encode(api_key.encode()).decode(),
        },
    }


def _build_pvc_manifest(
    name: str, namespace: str, spec: dict, owner_ref: dict
) -> dict:
    persistence = spec.get("persistence", {})
    size = persistence.get("size", "1Gi")
    storage_class = persistence.get("storageClass", "")

    # NOTE: PVCs intentionally have NO ownerReference so they survive
    # Terminal CR deletion.  User workspace data must persist across
    # idle-reap / re-provision cycles.
    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": _resource_name(name, "pvc"),
            "namespace": namespace,
            "labels": _labels(name),
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
# Create handler
# ---------------------------------------------------------------------------


@kopf.on.create(GROUP, VERSION, PLURAL)
async def on_create(body, spec, name, namespace, patch, **_):
    """Create all child resources for a new Terminal CR."""
    log.info("Creating terminal %s/%s for user %s", namespace, name, spec.get("userId"))

    owner_ref = _owner_ref(body)
    api_key = _generate_api_key()
    core_v1 = k8s.CoreV1Api()

    # -- Status: Provisioning
    patch.status["phase"] = "Provisioning"
    patch.status["lastActivityAt"] = _now_iso()
    patch.status["conditions"] = _set_condition(
        {}, "Ready", "False", "Provisioning", "Creating child resources"
    )

    # -- PVC (if persistence enabled)
    persistence = spec.get("persistence", {})
    pvc_name = None
    if persistence.get("enabled", True):
        pvc_name = _resource_name(name, "pvc")
        pvc_manifest = _build_pvc_manifest(name, namespace, spec, owner_ref)
        try:
            core_v1.create_namespaced_persistent_volume_claim(
                namespace=namespace, body=pvc_manifest
            )
            log.info("Created PVC %s/%s", namespace, pvc_name)
        except k8s.exceptions.ApiException as e:
            if e.status == 409:
                log.info("PVC %s/%s already exists", namespace, pvc_name)
            else:
                raise

    # -- Secret (API key)
    secret_name = _resource_name(name, "apikey")
    secret_manifest = _build_secret_manifest(name, namespace, api_key, owner_ref)
    try:
        core_v1.create_namespaced_secret(namespace=namespace, body=secret_manifest)
        log.info("Created Secret %s/%s", namespace, secret_name)
    except k8s.exceptions.ApiException as e:
        if e.status == 409:
            log.info("Secret %s/%s already exists, reading existing key", namespace, secret_name)
            existing = core_v1.read_namespaced_secret(secret_name, namespace)
            api_key = base64.b64decode(existing.data["api-key"]).decode()
        else:
            raise

    # -- Service
    svc_name = _resource_name(name, "svc")
    svc_manifest = _build_service_manifest(name, namespace, owner_ref)
    try:
        core_v1.create_namespaced_service(namespace=namespace, body=svc_manifest)
        log.info("Created Service %s/%s", namespace, svc_name)
    except k8s.exceptions.ApiException as e:
        if e.status == 409:
            log.info("Service %s/%s already exists", namespace, svc_name)
        else:
            raise

    # -- Pod
    pod_name = _resource_name(name, "pod")
    pod_manifest = _build_pod_manifest(
        name, namespace, spec, api_key, owner_ref, pvc_name
    )
    try:
        core_v1.create_namespaced_pod(namespace=namespace, body=pod_manifest)
        log.info("Created Pod %s/%s", namespace, pod_name)
    except k8s.exceptions.ApiException as e:
        if e.status == 409:
            log.info("Pod %s/%s already exists", namespace, pod_name)
        else:
            raise

    # -- Update status
    service_url = f"http://{svc_name}.{namespace}.svc:8000"
    patch.status["podName"] = pod_name
    patch.status["serviceName"] = svc_name
    patch.status["serviceUrl"] = service_url
    patch.status["apiKeySecret"] = secret_name
    patch.status["phase"] = "Pending"
    patch.status["conditions"] = _set_condition(
        {"conditions": patch.status.get("conditions", [])},
        "Ready",
        "False",
        "PodNotReady",
        "Waiting for pod to become ready",
    )


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


@kopf.on.event("v1", "pods", labels={"app.kubernetes.io/managed-by": "open-terminal-operator"})
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

    new_phase = current_phase
    if is_ready and pod_phase == "Running":
        new_phase = "Running"
    elif pod_phase in ("Pending",):
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
        f"Pod phase: {pod_phase}",
    )

    status_patch = {
        "status": {
            "phase": new_phase,
            "conditions": conditions,
        }
    }

    if is_ready and new_phase == "Running":
        status_patch["status"]["lastActivityAt"] = _now_iso()

    try:
        custom_api.patch_namespaced_custom_object_status(
            group=GROUP,
            version=VERSION,
            namespace=namespace,
            plural=PLURAL,
            name=terminal_name,
            body=status_patch,
        )
    except k8s.exceptions.ApiException as e:
        if e.status == 404:
            return
        log.warning("Failed to patch Terminal %s/%s status: %s", namespace, terminal_name, e)


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

    # Delete the pod to free resources; the PVC, Secret, and CRD remain
    core_v1 = k8s.CoreV1Api()
    try:
        core_v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
    except k8s.exceptions.ApiException as e:
        if e.status == 404:
            log.info("Pod %s/%s already gone", namespace, pod_name)
        else:
            raise

    # Update status to Idle
    custom_api = k8s.CustomObjectsApi()
    try:
        custom_api.patch_namespaced_custom_object_status(
            group=GROUP,
            version=VERSION,
            namespace=namespace,
            plural=PLURAL,
            name=name,
            body={
                "status": {
                    "phase": "Idle",
                    "conditions": _set_condition(
                        status,
                        "Ready",
                        "False",
                        "IdleTimeout",
                        f"Pod deleted after {elapsed:.0f} min of inactivity",
                    ),
                }
            },
        )
    except k8s.exceptions.ApiException:
        pass
