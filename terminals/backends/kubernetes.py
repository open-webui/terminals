"""Kubernetes backend — provisions Open Terminal as Pods via the K8s API."""

import asyncio
import base64
import logging
import secrets
from typing import Optional

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiClient

from terminals.backends.base import Backend, parse_started_at
from terminals.config import settings
from terminals.utils.kubernetes_labels import template_label
from terminals.utils.template_specs import resolve_template_spec
from terminals.utils.network_profiles import resolve_network_profile
from terminals.utils.context import (
    CONTEXT_HASH_LABEL,
    CONTEXT_ID_ANNOTATION,
    CONTEXT_TYPE_LABEL,
    DEFAULT_CONTEXT_ID,
    context_hash,
    normalize_context_id,
    get_terminal_id,
)
from terminals.utils.env import build_terminal_env
from terminals.utils.kubernetes_security import (
    container_security_context,
    deep_merge,
    pod_security_context,
    restricted_enabled,
)
from terminals.utils.kubernetes_scheduling import node_selector, tolerations
from terminals.utils.image_pull import pull_policy

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEMPLATE_ID_ANNOTATION = "openwebui.com/template-id"


def _sanitize_name(template_id: str, context_id: str = DEFAULT_CONTEXT_ID) -> str:
    return f"terminal-{get_terminal_id(template_id, context_id)}"


def _parse_labels() -> dict[str, str]:
    """Parse ``TERMINALS_KUBERNETES_LABELS`` into a dict."""
    labels: dict[str, str] = {}
    if not settings.kubernetes_labels:
        return labels
    for pair in settings.kubernetes_labels.split(","):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            labels[k.strip()] = v.strip()
    return labels


def _base_labels(template_id: str, context_id: str) -> dict[str, str]:
    """Standard labels applied to every resource we create."""
    labels = {
        "app.kubernetes.io/managed-by": "terminals",
        "app.kubernetes.io/part-of": "open-terminal",
        "openwebui.com/instance": get_terminal_id(template_id, context_id),
    }
    labels.update(_parse_labels())
    return labels


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
                for field in ("readinessProbe", "livenessProbe"):
                    if field in template_container:
                        container[field] = template_container[field]
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


class KubernetesBackend(Backend):
    """Manage terminal environments as Kubernetes Pods + Services."""

    def __init__(self) -> None:
        super().__init__()
        self._api_client: Optional[ApiClient] = None

    async def _ensure_client(self) -> ApiClient:
        if self._api_client is None:
            if settings.kubernetes_kubeconfig:
                await config.load_kube_config(
                    config_file=settings.kubernetes_kubeconfig
                )
            else:
                config.load_incluster_config()
            self._api_client = ApiClient()
        return self._api_client

    # ------------------------------------------------------------------
    # Backend interface
    # ------------------------------------------------------------------

    async def provision(
        self,
        template_id: str,
        context_id: str = DEFAULT_CONTEXT_ID,
        spec: dict | None = None,
    ) -> dict:
        profile = resolve_network_profile(spec, "kubernetes")
        api_client = await self._ensure_client()
        core = client.CoreV1Api(api_client)
        s = spec or {}

        api_key = secrets.token_urlsafe(24)
        context_id = normalize_context_id(context_id)
        name = _sanitize_name(template_id, context_id)
        template_slug = template_label(template_id)
        ns = settings.kubernetes_namespace
        labels = _base_labels(template_id, context_id)
        labels.update(profile.get("kubernetes_labels", {}))
        labels["openwebui.com/template"] = template_slug
        labels[CONTEXT_TYPE_LABEL] = (
            DEFAULT_CONTEXT_ID
            if context_id == DEFAULT_CONTEXT_ID
            else context_id.split(":", 1)[0]
        )
        labels[CONTEXT_HASH_LABEL] = context_hash(context_id)
        annotations = {
            _TEMPLATE_ID_ANNOTATION: template_id,
            CONTEXT_ID_ANNOTATION: context_id,
        }

        image = s.get("image", settings.effective_kubernetes_image)
        storage_mode = s.get("storage_mode", settings.kubernetes_storage_mode)
        storage_size = s.get("storage", settings.kubernetes_storage_size)

        # ---- Secret (API key) --------------------------------------------
        secret_name = f"{name}-apikey"
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name=secret_name, namespace=ns, labels=labels),
            string_data={"api-key": api_key},
        )
        try:
            await core.create_namespaced_secret(ns, secret)
            log.info("Created Secret %s in %s", secret_name, ns)
        except client.exceptions.ApiException as exc:
            if exc.status == 409:
                # Secret exists — delete and recreate with new key.
                await core.delete_namespaced_secret(secret_name, ns)
                await core.create_namespaced_secret(ns, secret)
                log.info("Replaced Secret %s in %s", secret_name, ns)
            else:
                raise

        # ---- Env vars ----------------------------------------------------
        env_vars = [
            client.V1EnvVar(
                name="OPEN_TERMINAL_API_KEY",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=secret_name, key="api-key",
                    ),
                ),
            ),
        ]
        terminal_env = build_terminal_env(
            s.get("env", {}),
            cpu_limit=s.get("cpu_limit"),
            memory_limit=s.get("memory_limit"),
        )
        for k, v in terminal_env.items():
            env_vars.append(client.V1EnvVar(name=k, value=str(v)))

        # Egress filtering is handled inside the container (dnsmasq + ipset +
        # iptables + capsh) triggered by OPEN_TERMINAL_ALLOWED_DOMAINS env var.
        has_egress_template = "OPEN_TERMINAL_ALLOWED_DOMAINS" in terminal_env
        restricted = restricted_enabled(s)
        pod_security = pod_security_context(s)
        container_security = container_security_context(s)
        if not container_security and has_egress_template and not restricted:
            container_security = {
                "capabilities": {
                    "add": ["NET_ADMIN"],
                },
            }

        # ---- Resource requirements ---------------------------------------
        resource_reqs = None
        limits = {}
        if s.get("cpu_limit"):
            limits["cpu"] = s["cpu_limit"]
        if s.get("memory_limit"):
            limits["memory"] = s["memory_limit"]
        if limits:
            resource_reqs = client.V1ResourceRequirements(limits=limits)

        # ---- Storage (3 modes) -------------------------------------------
        volumes = []
        volume_mounts = []
        affinity = None
        shared_pvc_name = f"terminals-shared-{ns}"
        shared_sub_path = f".terminals/{get_terminal_id(template_id, context_id)}"

        if storage_size:
            if storage_mode == "per-user":
                # Legacy storage-mode spelling; each environment gets its own PVC.
                pvc = client.V1PersistentVolumeClaim(
                    metadata=client.V1ObjectMeta(name=name, namespace=ns, labels=labels),
                    spec=client.V1PersistentVolumeClaimSpec(
                        access_modes=["ReadWriteOnce"],
                        resources=client.V1VolumeResourceRequirements(
                            requests={"storage": storage_size},
                        ),
                        **(
                            {"storage_class_name": settings.kubernetes_storage_class}
                            if settings.kubernetes_storage_class
                            else {}
                        ),
                    ),
                )
                try:
                    await core.create_namespaced_persistent_volume_claim(ns, pvc)
                    log.info("Created PVC %s in %s", name, ns)
                except client.exceptions.ApiException as exc:
                    if exc.status != 409:
                        raise
                volumes.append(
                    client.V1Volume(
                        name="home",
                        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                            claim_name=name,
                        ),
                    )
                )
                volume_mounts.append(
                    client.V1VolumeMount(name="home", mount_path="/home/user"),
                )

            elif storage_mode == "shared":
                # Single RWX PVC shared across all users, subPath per user
                await self._ensure_shared_pvc(core, ns, shared_pvc_name, storage_size, "ReadWriteMany")
                volumes.append(
                    client.V1Volume(
                        name="home",
                        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                            claim_name=shared_pvc_name,
                        ),
                    )
                )
                volume_mounts.append(
                    client.V1VolumeMount(
                        name="home",
                        mount_path="/home/user",
                        sub_path=shared_sub_path,
                    ),
                )

            elif storage_mode == "shared-rwo":
                # Single RWO PVC, all pods on same node
                await self._ensure_shared_pvc(core, ns, shared_pvc_name, storage_size, "ReadWriteOnce")
                volumes.append(
                    client.V1Volume(
                        name="home",
                        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                            claim_name=shared_pvc_name,
                        ),
                    )
                )
                volume_mounts.append(
                    client.V1VolumeMount(
                        name="home",
                        mount_path="/home/user",
                        sub_path=shared_sub_path,
                    ),
                )

        # ---- Pod ---------------------------------------------------------
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=ns,
                labels=labels,
                annotations=annotations,
            ),
            spec=client.V1PodSpec(
                containers=[
                    client.V1Container(
                        name="open-terminal",
                        image=image,
                        image_pull_policy=pull_policy(image),
                        ports=[client.V1ContainerPort(container_port=8000)],
                        env=env_vars,
                        volume_mounts=volume_mounts or None,
                        resources=resource_reqs,
                        security_context=container_security or None,
                        readiness_probe=client.V1Probe(
                            http_get=client.V1HTTPGetAction(
                                path="/health", port=8000
                            ),
                            initial_delay_seconds=3,
                            period_seconds=5,
                        ),
                    )
                ],
                volumes=volumes or None,
                affinity=affinity,
                node_selector=node_selector(),
                tolerations=tolerations(),
                restart_policy="Always",
                security_context=pod_security or None,
            ),
        )
        pod = _apply_pod_template(
            api_client.sanitize_for_serialization(pod),
            s.get("pod_template") or s.get("podTemplate"),
        )

        try:
            await core.create_namespaced_pod(ns, pod)
            log.info("Created Pod %s in %s (template=%s, storage=%s)", name, ns, template_id, storage_mode)
        except client.exceptions.ApiException as exc:
            if exc.status == 409:
                log.info("Pod %s already exists, replacing", name)
                await core.delete_namespaced_pod(name, ns)
                await self._wait_for_pod_deletion(core, name, ns)
                await core.create_namespaced_pod(ns, pod)
            else:
                raise


        # ---- Service -----------------------------------------------------
        svc = client.V1Service(
            metadata=client.V1ObjectMeta(name=name, namespace=ns, labels=labels),
            spec=client.V1ServiceSpec(
                type=settings.kubernetes_service_type,
                selector={
                    "openwebui.com/instance": get_terminal_id(template_id, context_id),
                    "openwebui.com/template": template_slug,
                    CONTEXT_HASH_LABEL: context_hash(context_id),
                },
                ports=[
                    client.V1ServicePort(port=8000, target_port=8000),
                ],
            ),
        )
        try:
            await core.create_namespaced_service(ns, svc)
            log.info("Created Service %s in %s", name, ns)
        except client.exceptions.ApiException as exc:
            if exc.status != 409:
                raise

        host = f"{name}.{ns}.svc.cluster.local"

        # Wait for the pod to be ready before returning.
        await self._wait_until_pod_ready(core, name, ns, timeout=60)

        return {
            "workload_name": name,
            "api_key": api_key,
            "host": host,
            "port": 8000,
        }

    async def _wait_until_pod_ready(
        self,
        core: client.CoreV1Api,
        name: str,
        ns: str,
        timeout: int = 60,
    ) -> None:
        """Poll until the pod's readiness probe passes."""

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                pod = await core.read_namespaced_pod(name, ns)
                conditions = pod.status.conditions or []
                for c in conditions:
                    if c.type == "Ready" and c.status == "True":
                        log.info("Pod %s is ready", name)
                        return
            except client.exceptions.ApiException:
                pass
            await asyncio.sleep(1)
        log.warning("Pod %s did not become ready within %ds", name, timeout)



        # Note: PVC is intentionally kept for data persistence.

    async def reset(
        self,
        template_id: str,
        context_id: str = DEFAULT_CONTEXT_ID,
        spec: dict | None = None,
    ) -> None:
        api_client = await self._ensure_client()
        core = client.CoreV1Api(api_client)
        ns = settings.kubernetes_namespace
        s = spec or {}
        storage_size = s.get("storage", settings.kubernetes_storage_size)
        if not storage_size:
            return

        storage_mode = s.get("storage_mode", settings.kubernetes_storage_mode)
        context_id = normalize_context_id(context_id)
        terminal_name = _sanitize_name(template_id, context_id)
        reset_name = f"{terminal_name[:57]}-reset"
        shared_pvc_name = f"terminals-shared-{ns}"
        claim_name = terminal_name
        sub_path = None
        if storage_mode in ("shared", "shared-rwo"):
            claim_name = shared_pvc_name
            sub_path = f".terminals/{get_terminal_id(template_id, context_id)}"

        try:
            await core.read_namespaced_persistent_volume_claim(claim_name, ns)
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return
            raise

        labels = _base_labels(template_id, context_id)
        labels["openwebui.com/template"] = template_label(template_id)
        labels[CONTEXT_TYPE_LABEL] = (
            DEFAULT_CONTEXT_ID
            if context_id == DEFAULT_CONTEXT_ID
            else context_id.split(":", 1)[0]
        )
        labels[CONTEXT_HASH_LABEL] = context_hash(context_id)
        labels["openwebui.com/reset"] = "true"
        labels.update(resolve_network_profile(s, "kubernetes").get("kubernetes_labels", {}))

        volume_mount = client.V1VolumeMount(
            name="home",
            mount_path="/home/user",
            sub_path=sub_path,
        )
        template = (s.get("pod_template") or s.get("podTemplate") or {}).get("spec") or {}
        terminal_container = next(
            (item for item in template.get("containers", []) if item.get("name") == "open-terminal"),
            {},
        )
        reset_image = s.get("image", settings.effective_kubernetes_image)
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(name=reset_name, namespace=ns, labels=labels),
            spec=client.V1PodSpec(
                restart_policy="Never",
                automount_service_account_token=False,
                security_context=deep_merge(
                    template.get("securityContext"), pod_security_context(s)
                ) or None,
                containers=[
                    client.V1Container(
                        name="reset",
                        image=reset_image,
                        image_pull_policy=pull_policy(reset_image),
                        security_context=deep_merge(
                            terminal_container.get("securityContext"), container_security_context(s)
                        ) or None,
                        resources=client.V1ResourceRequirements(
                            requests={"cpu": "10m", "memory": "16Mi"},
                            limits={"cpu": "100m", "memory": "64Mi"},
                        ),
                        command=[
                            "sh",
                            "-c",
                            "find /home/user -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +",
                        ],
                        volume_mounts=[volume_mount],
                    )
                ],
                volumes=[
                    client.V1Volume(
                        name="home",
                        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                            claim_name=claim_name,
                        ),
                    )
                ],
                node_selector=deep_merge(template.get("nodeSelector"), node_selector()) or None,
                tolerations=tolerations() or template.get("tolerations"),
                runtime_class_name=template.get("runtimeClassName"),
                priority_class_name=template.get("priorityClassName"),
                affinity=template.get("affinity"),
                node_name=template.get("nodeName"),
                scheduler_name=template.get("schedulerName"),
                topology_spread_constraints=template.get("topologySpreadConstraints"),
                image_pull_secrets=template.get("imagePullSecrets"),
                service_account_name=template.get("serviceAccountName"),
            ),
        )

        try:
            await core.delete_namespaced_pod(reset_name, ns)
            await self._wait_for_pod_deletion(core, reset_name, ns, timeout=30)
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise

        await core.create_namespaced_pod(ns, pod)
        await self._wait_for_reset_pod(core, reset_name, ns)
        try:
            await core.delete_namespaced_pod(reset_name, ns)
        except client.exceptions.ApiException:
            pass

    async def status(self, name: str) -> str:
        api_client = await self._ensure_client()
        core = client.CoreV1Api(api_client)
        ns = settings.kubernetes_namespace

        try:
            pod = await core.read_namespaced_pod(name, ns)
            phase = pod.status.phase  # Pending, Running, Succeeded, Failed, Unknown
            if phase == "Running":
                return "running"
            if phase == "Pending":
                # Check if truly starting vs stuck (unschedulable, image pull error, etc.)
                conditions = pod.status.conditions or []
                for c in conditions:
                    if c.type == "PodScheduled" and c.status == "False":
                        if c.reason == "Unschedulable":
                            log.warning("Pod %s is unschedulable: %s", name, c.message)
                            return "stopped"
                # Still pulling or initializing — treat as starting.
                return "running"
            return "stopped"
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise
            return "missing"

    async def close(self) -> None:
        if self._api_client is not None:
            await self._api_client.close()
            self._api_client = None

    # ------------------------------------------------------------------
    # Reconciliation — rediscover running pods on startup
    # ------------------------------------------------------------------

    async def reconcile(self) -> None:
        """Scan running K8s pods and repopulate ``_instances``.

        Called during startup to recover state after a restart.
        Reads full template/context identifiers from annotations and the API key
        from the companion Secret.
        """
        api_client = await self._ensure_client()
        core = client.CoreV1Api(api_client)
        ns = settings.kubernetes_namespace

        try:
            pods = await core.list_namespaced_pod(
                ns, label_selector="app.kubernetes.io/managed-by=terminals"
            )
        except client.exceptions.ApiException:
            log.warning("Failed to list pods for reconciliation")
            return

        recovered = 0
        adopted_specs: dict[str, dict] = {}
        for pod in pods.items:
            if pod.status.phase not in ("Running", "Pending"):
                continue

            annotations = pod.metadata.annotations or {}
            template_id = annotations.get(_TEMPLATE_ID_ANNOTATION)
            context_id = annotations.get(CONTEXT_ID_ANNOTATION)
            if not template_id or not context_id:
                log.warning("Skipping pod %s without template/context metadata", pod.metadata.name)
                continue
            if pod.metadata.name != _sanitize_name(template_id, context_id):
                continue

            name = pod.metadata.name

            key = self._key(template_id, context_id)
            if key in self._instances:
                continue

            # Read API key from the companion Secret
            secret_name = f"{name}-apikey"
            api_key = None
            try:
                secret = await core.read_namespaced_secret(secret_name, ns)
                raw = secret.data.get("api-key", "")
                api_key = base64.b64decode(raw).decode() if raw else None
            except client.exceptions.ApiException:
                pass

            if not api_key:
                log.debug("Pod %s has no API key secret, skipping", name)
                continue

            host = f"{name}.{ns}.svc.cluster.local"
            if template_id not in adopted_specs:
                adopted_specs[template_id] = await resolve_template_spec(template_id)
            self._instances[key] = {
                "workload_name": name,
                "api_key": api_key,
                "host": host,
                "port": 8000,
                "started_at": parse_started_at(pod.status.start_time),
            }
            self._specs[key] = adopted_specs[template_id]
            self._instances[key].update(template_id=template_id, context_id=context_id)
            recovered += 1

        if recovered:
            log.info("Reconciled %d running pod(s)", recovered)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _wait_for_pod_deletion(
        self, core: client.CoreV1Api, name: str, ns: str, timeout: int = 30,
    ) -> None:
        """Wait until a pod is fully deleted."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                await core.read_namespaced_pod(name, ns)
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    return
                raise
            await asyncio.sleep(0.5)
        log.warning("Pod %s not fully deleted after %ds", name, timeout)

    async def _wait_for_reset_pod(
        self, core: client.CoreV1Api, name: str, ns: str, timeout: int = 120,
    ) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            pod = await core.read_namespaced_pod(name, ns)
            phase = pod.status.phase
            if phase == "Succeeded":
                return
            if phase == "Failed":
                raise RuntimeError(f"Reset pod {name} failed")
            await asyncio.sleep(1)
        raise TimeoutError(f"Reset pod {name} did not finish within {timeout}s")

    async def _ensure_shared_pvc(
        self,
        core: client.CoreV1Api,
        ns: str,
        name: str,
        size: str,
        access_mode: str,
    ) -> None:
        """Create the shared PVC if it doesn't already exist."""
        pvc = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=ns,
                labels={
                    "app.kubernetes.io/managed-by": "terminals",
                    "app.kubernetes.io/part-of": "open-terminal",
                },
            ),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=[access_mode],
                resources=client.V1VolumeResourceRequirements(
                    requests={"storage": size},
                ),
                **(
                    {"storage_class_name": settings.kubernetes_storage_class}
                    if settings.kubernetes_storage_class
                    else {}
                ),
            ),
        )
        try:
            await core.create_namespaced_persistent_volume_claim(ns, pvc)
            log.info("Created shared PVC %s in %s (%s)", name, ns, access_mode)
        except client.exceptions.ApiException as exc:
            if exc.status != 409:
                raise
