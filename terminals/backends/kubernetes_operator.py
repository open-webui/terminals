"""Kubernetes Operator backend — manages Terminals via CRDs.

Instead of creating Pods/Services directly, this backend creates and manages
``Terminal`` custom resources.  A separate Kopf-based operator watches these
CRs and reconciles the underlying Pods, Services, Secrets, and PVCs.

The operator generates API keys and stores them in Kubernetes Secrets.
This backend reads the key from the Secret referenced in ``status.apiKeySecret``.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
from typing import Optional

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiClient

from terminals.backends.base import Backend, RefreshResult
from terminals.config import settings
from terminals.utils.env import build_terminal_env
from terminals.utils.kubernetes_security import (
    container_security_context,
    pod_security_context,
    restricted_enabled,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DNS_SAFE = re.compile(r"[^a-z0-9-]")


def _sanitize_name(user_id: str, policy_id: str = "default") -> str:
    """Deterministic, DNS-safe Terminal CR name from a user ID + policy."""
    short = hashlib.sha256(user_id.encode()).hexdigest()[:12]
    if policy_id == "default":
        return f"terminal-{short}"
    policy_slug = _DNS_SAFE.sub("-", policy_id.lower()).strip("-")[:20]
    return f"terminal-{short}-{policy_slug}"


def _json_env(name: str, default):
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Ignoring invalid JSON in %s", name)
        return default


def _pod_scheduling_overrides() -> dict:
    overrides = {}
    node_selector = _json_env("TERMINALS_KUBERNETES_NODE_SELECTOR", {})
    tolerations = _json_env("TERMINALS_KUBERNETES_TOLERATIONS", [])
    if isinstance(node_selector, dict) and node_selector:
        overrides["node_selector"] = {str(k): str(v) for k, v in node_selector.items()}
    if isinstance(tolerations, list) and tolerations:
        overrides["tolerations"] = tolerations
    return overrides


class KubernetesOperatorBackend(Backend):
    """Manage terminal instances via Terminal CRDs.

    The backend creates/deletes ``Terminal`` custom resources in the
    configured namespace.  A Kopf operator running in the cluster watches
    these resources and manages the actual Pods, Services, Secrets, and PVCs.
    """

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

    @property
    def _group(self) -> str:
        return settings.kubernetes_crd_group

    @property
    def _version(self) -> str:
        return settings.kubernetes_crd_version

    @property
    def _plural(self) -> str:
        return "terminals"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _read_api_key_from_secret(self, secret_name: str) -> Optional[str]:
        """Read the API key from a Kubernetes Secret."""
        api_client = await self._ensure_client()
        core = client.CoreV1Api(api_client)
        ns = settings.kubernetes_namespace
        try:
            secret = await core.read_namespaced_secret(secret_name, ns)
            raw = secret.data.get("api-key", "")
            return base64.b64decode(raw).decode() if raw else None
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return None
            raise

    async def _get_terminal_cr(
        self, user_id: str, policy_id: str = "default"
    ) -> Optional[dict]:
        """Get the Terminal CR for a user+policy, or None if it doesn't exist."""
        api_client = await self._ensure_client()
        custom = client.CustomObjectsApi(api_client)
        name = _sanitize_name(user_id, policy_id)
        ns = settings.kubernetes_namespace
        try:
            return await custom.get_namespaced_custom_object(
                group=self._group,
                version=self._version,
                namespace=ns,
                plural=self._plural,
                name=name,
            )
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return None
            raise

    async def _create_terminal_cr(
        self,
        user_id: str,
        policy_id: str = "default",
        spec: dict | None = None,
    ) -> dict:
        """Create a Terminal CR for a user+policy and return it."""
        api_client = await self._ensure_client()
        custom = client.CustomObjectsApi(api_client)
        name = _sanitize_name(user_id, policy_id)
        ns = settings.kubernetes_namespace
        s = spec or {}

        image = s.get("image", settings.kubernetes_image)
        storage_size = s.get("storage")  # absent = ephemeral

        # Build CRD spec aligned with manifests/terminal-crd.yaml.
        cr_spec: dict = {
            "userId": user_id,
            "image": image,
        }
        restricted = restricted_enabled(s)
        if restricted:
            cr_spec["restricted"] = True
        pod_security = pod_security_context(s)
        if pod_security:
            cr_spec["podSecurityContext"] = pod_security
        container_security = container_security_context(s)
        if container_security:
            cr_spec["containerSecurityContext"] = container_security

        # CPU / memory limits
        limits = {}
        if s.get("cpu_limit"):
            limits["cpu"] = s["cpu_limit"]
        if s.get("memory_limit"):
            limits["memory"] = s["memory_limit"]
        if limits:
            cr_spec["resources"] = {"limits": limits}

        # Storage: present = persistent, absent = ephemeral
        if storage_size:
            cr_spec["storage"] = storage_size
            if settings.kubernetes_storage_class:
                cr_spec["storageClass"] = settings.kubernetes_storage_class
            # Storage mode (per-user, shared, shared-rwo)
            storage_mode = s.get("storage_mode", settings.kubernetes_storage_mode)
            cr_spec["storageMode"] = storage_mode
            cr_spec["persistence"] = {
                "enabled": True,
                "size": storage_size,
                "storageClass": settings.kubernetes_storage_class,
            }
        else:
            cr_spec["persistence"] = {"enabled": False}

        # Env vars
        env = build_terminal_env(
            s.get("env", {}),
            cpu_limit=s.get("cpu_limit"),
            memory_limit=s.get("memory_limit"),
        )
        if env:
            cr_spec["env"] = env

        # Idle timeout
        idle_timeout = s.get("idle_timeout_minutes", settings.idle_timeout_minutes)
        if idle_timeout and idle_timeout > 0:
            cr_spec["idleTimeoutMinutes"] = idle_timeout

        policy_slug = _DNS_SAFE.sub("-", policy_id.lower()).strip("-")[:20]

        cr = {
            "apiVersion": f"{self._group}/{self._version}",
            "kind": "Terminal",
            "metadata": {
                "name": name,
                "namespace": ns,
                "labels": {
                    "app.kubernetes.io/managed-by": "terminals",
                    "app.kubernetes.io/part-of": "open-terminal",
                    "openwebui.com/user-id": user_id,
                    "openwebui.com/policy": policy_slug,
                },
            },
            "spec": cr_spec,
        }

        try:
            return await custom.create_namespaced_custom_object(
                group=self._group,
                version=self._version,
                namespace=ns,
                plural=self._plural,
                body=cr,
            )
        except client.exceptions.ApiException as exc:
            if exc.status == 409:
                # Already exists — but may be mid-deletion (finalizer pending).
                existing = await self._get_terminal_cr(user_id, policy_id)
                if existing and existing.get("metadata", {}).get("deletionTimestamp"):
                    # CR is being deleted; wait for it to vanish, then retry create
                    await self._wait_for_deletion(user_id, policy_id, timeout=60)
                    return await custom.create_namespaced_custom_object(
                        group=self._group,
                        version=self._version,
                        namespace=ns,
                        plural=self._plural,
                        body=cr,
                    )
                if existing:
                    return existing
                # Gone between the 409 and our GET — safe to retry
                return await custom.create_namespaced_custom_object(
                    group=self._group,
                    version=self._version,
                    namespace=ns,
                    plural=self._plural,
                    body=cr,
                )
            raise

    async def _delete_terminal_cr(
        self,
        user_id: str,
        policy_id: str = "default",
        wait: bool = True,
        timeout: int = 60,
    ) -> bool:
        """Delete the Terminal CR for a user+policy and optionally wait for it to be gone.

        When *wait* is True (default), polls until the CR returns 404 so that
        a subsequent create won't collide with the kopf finalizer.
        """
        api_client = await self._ensure_client()
        custom = client.CustomObjectsApi(api_client)
        name = _sanitize_name(user_id, policy_id)
        ns = settings.kubernetes_namespace
        try:
            await custom.delete_namespaced_custom_object(
                group=self._group,
                version=self._version,
                namespace=ns,
                plural=self._plural,
                name=name,
            )
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return False
            raise

        if not wait:
            return True

        # Poll until the CR is fully removed (finalizer may delay deletion)
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                await custom.get_namespaced_custom_object(
                    group=self._group,
                    version=self._version,
                    namespace=ns,
                    plural=self._plural,
                    name=name,
                )
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    return True
                raise
            await asyncio.sleep(1)

        log.warning("Terminal CR %s not fully deleted after %ds", name, timeout)
        return True

    async def _wait_for_deletion(
        self, user_id: str, policy_id: str = "default", timeout: int = 60
    ) -> None:
        """Poll until a Terminal CR no longer exists (404)."""
        api_client = await self._ensure_client()
        custom = client.CustomObjectsApi(api_client)
        name = _sanitize_name(user_id, policy_id)
        ns = settings.kubernetes_namespace

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                await custom.get_namespaced_custom_object(
                    group=self._group,
                    version=self._version,
                    namespace=ns,
                    plural=self._plural,
                    name=name,
                )
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    return
                raise
            await asyncio.sleep(1)

        log.warning("Terminal CR %s still exists after %ds wait", name, timeout)

    async def _wait_for_ready(
        self, name: str, namespace: str, timeout: int = 120
    ) -> Optional[dict]:
        """Poll the CR status until Running with serviceUrl and apiKeySecret.

        Returns a dict with ``service_url`` and ``api_key``, or None on timeout.
        """
        api_client = await self._ensure_client()
        custom = client.CustomObjectsApi(api_client)

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                cr = await custom.get_namespaced_custom_object(
                    group=self._group,
                    version=self._version,
                    namespace=namespace,
                    plural=self._plural,
                    name=name,
                )
                status = cr.get("status", {})
                if (
                    status.get("phase") == "Running"
                    and status.get("serviceUrl")
                    and status.get("apiKeySecret")
                ):
                    api_key = await self._read_api_key_from_secret(status["apiKeySecret"])
                    if api_key:
                        return {
                            "service_url": status["serviceUrl"],
                            "api_key": api_key,
                        }
            except client.exceptions.ApiException:
                pass
            await asyncio.sleep(2)

        log.warning(
            "Terminal CR %s did not reach Running in %ds",
            name,
            timeout,
        )
        return None

    async def _name_from_uid(self, uid: str) -> Optional[str]:
        """Look up a Terminal CR name by its UID."""
        api_client = await self._ensure_client()
        custom = client.CustomObjectsApi(api_client)
        ns = settings.kubernetes_namespace

        try:
            result = await custom.list_namespaced_custom_object(
                group=self._group,
                version=self._version,
                namespace=ns,
                plural=self._plural,
                label_selector="app.kubernetes.io/managed-by=terminals",
            )
            for item in result.get("items", []):
                if item["metadata"]["uid"] == uid:
                    return item["metadata"]["name"]
        except client.exceptions.ApiException:
            pass
        return None

    def _parse_service_url(self, service_url: str) -> tuple[str, int]:
        """Extract host and port from a service URL like http://svc:8000."""
        url = service_url.rstrip("/")
        if "://" in url:
            url = url.split("://", 1)[1]
        if ":" in url:
            host, port_str = url.rsplit(":", 1)
            return host, int(port_str)
        return url, 8000

    async def _wait_for_reset_pod(
        self, core: client.CoreV1Api, name: str, namespace: str, timeout: int = 120
    ) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            pod = await core.read_namespaced_pod(name, namespace)
            phase = pod.status.phase
            if phase == "Succeeded":
                return
            if phase == "Failed":
                raise RuntimeError(f"Reset pod {name} failed")
            await asyncio.sleep(1)
        raise TimeoutError(f"Reset pod {name} did not finish within {timeout}s")

    async def _wait_for_pod_deletion(
        self, core: client.CoreV1Api, name: str, namespace: str, timeout: int = 30
    ) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                await core.read_namespaced_pod(name, namespace)
            except client.exceptions.ApiException as exc:
                if exc.status == 404:
                    return
                raise
            await asyncio.sleep(0.5)

    async def reset(
        self, user_id: str, policy_id: str, spec: dict | None = None
    ) -> None:
        api_client = await self._ensure_client()
        core = client.CoreV1Api(api_client)
        ns = settings.kubernetes_namespace
        terminal_name = _sanitize_name(user_id, policy_id)
        reset_name = f"{terminal_name[:57]}-reset"
        claim_name = f"{terminal_name}-pvc"

        try:
            await core.read_namespaced_persistent_volume_claim(claim_name, ns)
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return
            raise

        labels = {
            "app.kubernetes.io/managed-by": "terminals",
            "app.kubernetes.io/part-of": "open-terminal",
            "openwebui.com/user-id": user_id,
            "openwebui.com/policy": _DNS_SAFE.sub("-", policy_id.lower()).strip("-")[:20],
            "openwebui.com/reset": "true",
        }
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(name=reset_name, namespace=ns, labels=labels),
            spec=client.V1PodSpec(
                restart_policy="Never",
                **_pod_scheduling_overrides(),
                containers=[
                    client.V1Container(
                        name="reset",
                        image="busybox:1.36",
                        command=[
                            "sh",
                            "-c",
                            "find /home/user -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +",
                        ],
                        volume_mounts=[
                            client.V1VolumeMount(name="home", mount_path="/home/user")
                        ],
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
            ),
        )

        try:
            await core.delete_namespaced_pod(reset_name, ns)
            await self._wait_for_pod_deletion(core, reset_name, ns)
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise

        await core.create_namespaced_pod(ns, pod)
        await self._wait_for_reset_pod(core, reset_name, ns)
        try:
            await core.delete_namespaced_pod(reset_name, ns)
        except client.exceptions.ApiException:
            pass

    async def refresh(
        self,
        *,
        user_id: str | None = None,
        policy_id: str | None = None,
        only_idle: bool = True,
        reset: bool = False,
    ) -> RefreshResult:
        api_client = await self._ensure_client()
        custom = client.CustomObjectsApi(api_client)
        ns = settings.kubernetes_namespace
        result = RefreshResult()

        selector = "app.kubernetes.io/managed-by=terminals"
        crs = await custom.list_namespaced_custom_object(
            group=self._group,
            version=self._version,
            namespace=ns,
            plural=self._plural,
            label_selector=selector,
        )
        for item in crs.get("items", []):
            labels = item.get("metadata", {}).get("labels", {})
            item_user = labels.get("openwebui.com/user-id") or item.get("spec", {}).get("userId")
            item_policy = labels.get("openwebui.com/policy", "default")
            if user_id and item_user != user_id:
                continue
            if policy_id and item_policy != policy_id:
                continue

            result.matched += 1
            phase = (item.get("status") or {}).get("phase")
            if only_idle and phase == "Running":
                result.skipped_active += 1
                continue

            name = item["metadata"]["name"]
            await custom.delete_namespaced_custom_object(
                group=self._group,
                version=self._version,
                namespace=ns,
                plural=self._plural,
                name=name,
            )
            result.refreshed += 1

            if reset and item_user:
                await self.reset(item_user, item_policy, item.get("spec") or {})
                result.reset += 1

        return result

    # ------------------------------------------------------------------
    # Backend interface
    # ------------------------------------------------------------------

    async def provision(
        self,
        user_id: str,
        policy_id: str = "default",
        spec: dict | None = None,
    ) -> Optional[dict]:
        """Create a Terminal CR and wait for it to become ready.

        Returns connection info dict or ``None`` on timeout.
        """
        cr = await self._create_terminal_cr(user_id, policy_id=policy_id, spec=spec)
        name = cr["metadata"]["name"]
        ns = settings.kubernetes_namespace

        ready = await self._wait_for_ready(name, ns, timeout=120)
        if ready:
            host, port = self._parse_service_url(ready["service_url"])
            return {
                "instance_id": cr["metadata"]["uid"],
                "instance_name": name,
                "api_key": ready["api_key"],
                "host": host,
                "port": port,
            }

        return None

    async def start(self, instance_id: str) -> bool:
        """For idle terminals, delete and re-create the CR."""
        name = await self._name_from_uid(instance_id)
        if name is None:
            return False

        api_client = await self._ensure_client()
        custom = client.CustomObjectsApi(api_client)
        ns = settings.kubernetes_namespace

        try:
            cr = await custom.get_namespaced_custom_object(
                group=self._group,
                version=self._version,
                namespace=ns,
                plural=self._plural,
                name=name,
            )
        except client.exceptions.ApiException:
            return False

        phase = cr.get("status", {}).get("phase")
        if phase == "Running":
            return True
        if phase in ("Pending", "Provisioning"):
            return True  # still coming up

        # Idle or Error means the caller should refresh this CR.
        return False

    async def teardown(self, instance_id: str) -> None:
        api_client = await self._ensure_client()
        custom = client.CustomObjectsApi(api_client)
        ns = settings.kubernetes_namespace

        name = await self._name_from_uid(instance_id)
        if name is None:
            log.warning("No Terminal CR found for UID %s", instance_id)
            return

        try:
            await custom.delete_namespaced_custom_object(
                group=self._group,
                version=self._version,
                namespace=ns,
                plural=self._plural,
                name=name,
            )
            log.info("Deleted Terminal CR %s", name)
        except client.exceptions.ApiException:
            log.warning(
                "Could not delete Terminal CR %s (may already be gone)", name
            )

    async def status(self, instance_id: str) -> str:
        api_client = await self._ensure_client()
        custom = client.CustomObjectsApi(api_client)
        ns = settings.kubernetes_namespace

        name = await self._name_from_uid(instance_id)
        if name is None:
            return "missing"

        try:
            cr = await custom.get_namespaced_custom_object(
                group=self._group,
                version=self._version,
                namespace=ns,
                plural=self._plural,
                name=name,
            )
            phase = cr.get("status", {}).get("phase", "Unknown")
            if phase == "Running":
                return "running"
            if phase in ("Provisioning", "Pending"):
                return "running"  # still coming up
            if phase == "Idle":
                return "stopped"
            return "stopped"
        except client.exceptions.ApiException:
            return "missing"

    async def close(self) -> None:
        if self._api_client is not None:
            await self._api_client.close()
            self._api_client = None

    # ------------------------------------------------------------------
    # Operator-aware ensure_terminal
    # ------------------------------------------------------------------

    async def ensure_terminal(
        self,
        user_id: str,
        policy_id: str = "default",
        spec: Optional[dict] = None,
    ) -> Optional[dict]:
        """Get or create a terminal, resolving from K8s CRDs.

        Uses a per-key lock so concurrent requests for the same user+policy
        don't race to create the same CR.

        Returns a dict with ``api_key``, ``host``, ``port`` or ``None``.
        """
        key = self._key(user_id, policy_id)

        # Fast path: check if CR is already Running without taking the lock.
        cr = await self._get_terminal_cr(user_id, policy_id)
        if cr:
            status = cr.get("status") or {}
            phase = status.get("phase")
            if phase == "Running" and status.get("serviceUrl") and status.get("apiKeySecret"):
                api_key = await self._read_api_key_from_secret(status["apiKeySecret"])
                if api_key:
                    host, port = self._parse_service_url(status["serviceUrl"])
                    return {
                        "instance_id": cr["metadata"]["uid"],
                        "instance_name": cr["metadata"]["name"],
                        "api_key": api_key,
                        "host": host,
                        "port": port,
                    }

        # Serialise provisioning per key.
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()

        async with self._locks[key]:
            # Re-check after acquiring lock.
            cr = await self._get_terminal_cr(user_id, policy_id)

            if cr is None:
                await self._apply_due_reset(user_id, policy_id, spec)
                return await self.provision(user_id, policy_id=policy_id, spec=spec)

            status = cr.get("status") or {}
            phase = status.get("phase")

            if phase in ("Idle", "Error"):
                log.info(
                    "Terminal CR %s in phase %s, deleting before refresh",
                    cr["metadata"]["name"],
                    phase,
                )
                await self._delete_terminal_cr(user_id, policy_id)
                await self._apply_due_reset(user_id, policy_id, spec)
                return await self.provision(user_id, policy_id=policy_id, spec=spec)

            if phase == "Running" and status.get("serviceUrl") and status.get("apiKeySecret"):
                api_key = await self._read_api_key_from_secret(status["apiKeySecret"])
                if api_key:
                    host, port = self._parse_service_url(status["serviceUrl"])
                    return {
                        "instance_id": cr["metadata"]["uid"],
                        "instance_name": cr["metadata"]["name"],
                        "api_key": api_key,
                        "host": host,
                        "port": port,
                    }

            # Still provisioning — wait for the operator to bring it up
            name = cr["metadata"]["name"]
            ns = settings.kubernetes_namespace
            ready = await self._wait_for_ready(name, ns, timeout=120)
            if ready:
                host, port = self._parse_service_url(ready["service_url"])
                return {
                    "instance_id": cr["metadata"]["uid"],
                    "instance_name": cr["metadata"]["name"],
                    "api_key": ready["api_key"],
                    "host": host,
                    "port": port,
                }

            return None

    async def get_terminal_info(self, user_id: str) -> Optional[dict]:
        """Look up an existing terminal from the K8s CRD without creating one."""
        cr = await self._get_terminal_cr(user_id)
        if cr is None:
            return None

        status = cr.get("status") or {}
        phase = status.get("phase")

        if phase == "Running" and status.get("serviceUrl") and status.get("apiKeySecret"):
            api_key = await self._read_api_key_from_secret(status["apiKeySecret"])
            if api_key:
                host, port = self._parse_service_url(status["serviceUrl"])
                return {
                    "instance_id": cr["metadata"]["uid"],
                    "instance_name": cr["metadata"]["name"],
                    "api_key": api_key,
                    "host": host,
                    "port": port,
                }

        return None

    async def touch_activity(
        self, user_id: str, policy_id: str = "default"
    ) -> None:
        """Update lastActivityAt on the Terminal CR to prevent idle culling."""
        api_client = await self._ensure_client()
        custom = client.CustomObjectsApi(api_client)
        name = _sanitize_name(user_id, policy_id)
        ns = settings.kubernetes_namespace
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            await custom.patch_namespaced_custom_object_status(
                group=self._group,
                version=self._version,
                namespace=ns,
                plural=self._plural,
                name=name,
                body={"status": {"lastActivityAt": now}},
                _content_type="application/merge-patch+json",
            )
        except client.exceptions.ApiException as e:
            if e.status != 404:
                log.warning("Failed to touch activity for %s: %s", name, e)
