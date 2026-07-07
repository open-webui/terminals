"""Kubernetes Agent Sandbox backend — manages terminals via the upstream
`agent-sandbox <https://github.com/kubernetes-sigs/agent-sandbox>`_ ``Sandbox`` CRD.

Each user+policy maps to a single ``Sandbox`` (``agents.x-k8s.io/v1beta1``): the
agent-sandbox controller reconciles it into a Pod, a headless Service (giving a
stable ``serviceFQDN``), and — when a workspace is requested — a PersistentVolume.
This backend only creates/patches/deletes ``Sandbox`` objects; the controller
(installed separately) does the rest.

Lifecycle uses the Sandbox ``operatingMode`` field for idle handling: on idle we
patch the Sandbox to ``operatingMode: Suspended`` (scale-to-zero, identity +
workspace preserved); on the next request we patch it back to ``operatingMode:
Running``.  Teardown deletes the Sandbox.

The per-user Open Terminal API key is generated here and baked into the Sandbox's
pod template env (``OPEN_TERMINAL_API_KEY``); it is read back from the Sandbox object
when resolving connection info, so the backend stays stateless across restarts.

Targets the agent-sandbox ``v1beta1`` API (v0.5.0+).  The capabilities this backend
still self-manages — deciding *when* to suspend based on request activity, and
generating the per-user key — are on the upstream roadmap (``Auto Suspend/Resume``,
``Scale to Zero``, ``Sandbox/Pod Identity Association``); we can drop those shims as
the controller gains them.
"""

import asyncio
import hashlib
import logging
import re
import secrets
import string
import time
from typing import Optional

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiClient

from terminals.backends.base import Backend
from terminals.config import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DNS_SAFE = re.compile(r"[^a-z0-9-]")

_PLURAL_SANDBOX = "sandboxes"
_API_KEY_ENV = "OPEN_TERMINAL_API_KEY"
_CONTAINER_NAME = "open-terminal"
_MANAGED_BY = "app.kubernetes.io/managed-by=terminals"
# Exact identity for reconcile(); the Sandbox name is a hash and label values
# are charset-limited, so the un-mangled user/policy ids live in annotations.
_ANN_USER_ID = "openwebui.com/user-id"
_ANN_POLICY_ID = "openwebui.com/policy-id"


def _policy_slug(policy_id: str) -> str:
    """DNS-safe slug for a policy id."""
    return _DNS_SAFE.sub("-", policy_id.lower()).strip("-")[:20] or "default"


def _sandbox_name(user_id: str, policy_id: str = "default") -> str:
    """Deterministic, DNS-safe Sandbox name from a user id + policy.

    Mirrors the previous operator backend's scheme so names stay stable and
    within the 63-character DNS label limit.
    """
    short = hashlib.sha256(user_id.encode()).hexdigest()[:12]
    if policy_id == "default":
        return f"term-{short}"
    return f"term-{short}-{_policy_slug(policy_id)}"


def _generate_api_key(length: int = 48) -> str:
    alphabet = string.ascii_letters + string.digits
    return "sk-" + "".join(secrets.choice(alphabet) for _ in range(length))


def _labels(user_id: str = "", policy_id: str = "default") -> dict[str, str]:
    labels = {
        "app.kubernetes.io/name": "open-terminal",
        "app.kubernetes.io/managed-by": "terminals",
        "app.kubernetes.io/part-of": "open-terminal",
        "openwebui.com/policy": _policy_slug(policy_id),
    }
    if user_id:
        labels["openwebui.com/user-id"] = user_id
    return labels


class KubernetesSandboxBackend(Backend):
    """Manage terminal instances via Agent Sandbox ``Sandbox`` objects."""

    def __init__(self) -> None:
        super().__init__()
        self._api_client: Optional[ApiClient] = None

    # ------------------------------------------------------------------
    # Client / config plumbing
    # ------------------------------------------------------------------

    async def _ensure_client(self) -> ApiClient:
        if self._api_client is None:
            if settings.kubernetes_kubeconfig:
                await config.load_kube_config(config_file=settings.kubernetes_kubeconfig)
            else:
                config.load_incluster_config()
            self._api_client = ApiClient()
        return self._api_client

    async def _custom(self) -> client.CustomObjectsApi:
        return client.CustomObjectsApi(await self._ensure_client())

    @property
    def _ns(self) -> str:
        return settings.kubernetes_namespace

    @property
    def _group(self) -> str:
        return settings.sandbox_core_group

    @property
    def _version(self) -> str:
        return settings.sandbox_version

    # ------------------------------------------------------------------
    # Manifest builders
    # ------------------------------------------------------------------

    def _build_pod_template(self, spec: dict, api_key: str) -> dict:
        """Build the Sandbox ``podTemplate`` from a policy spec + per-user key."""
        image = spec.get("image", settings.kubernetes_image)
        port = settings.sandbox_port

        env = [
            {"name": _API_KEY_ENV, "value": api_key},
            {"name": "OPEN_TERMINAL_HOST", "value": "0.0.0.0"},
            {"name": "OPEN_TERMINAL_PORT", "value": str(port)},
        ]
        for k, v in (spec.get("env") or {}).items():
            env.append({"name": k, "value": str(v)})

        container: dict = {
            "name": _CONTAINER_NAME,
            "image": image,
            "ports": [{"containerPort": port, "name": "http", "protocol": "TCP"}],
            "env": env,
            "readinessProbe": {
                "httpGet": {"path": "/health", "port": port},
                "initialDelaySeconds": 3,
                "periodSeconds": 5,
            },
            "livenessProbe": {
                "httpGet": {"path": "/health", "port": port},
                "initialDelaySeconds": 10,
                "periodSeconds": 15,
            },
        }

        requests = {"cpu": "100m", "memory": "256Mi"}
        limits = {}
        if spec.get("cpu_limit"):
            limits["cpu"] = spec["cpu_limit"]
        if spec.get("memory_limit"):
            limits["memory"] = spec["memory_limit"]
        container["resources"] = {"requests": requests}
        if limits:
            container["resources"]["limits"] = limits

        if spec.get("storage"):
            container["volumeMounts"] = [
                {"name": "workspace", "mountPath": "/workspace"}
            ]

        pod_spec: dict = {
            "containers": [container],
            "restartPolicy": "Always",
            "enableServiceLinks": False,
            "automountServiceAccountToken": False,
        }
        if settings.sandbox_runtime_class:
            pod_spec["runtimeClassName"] = settings.sandbox_runtime_class

        return {"spec": pod_spec}

    def _build_volume_claim_templates(self, spec: dict) -> list[dict]:
        """Build ``volumeClaimTemplates`` (empty when storage is ephemeral)."""
        size = spec.get("storage")
        if not size:
            return []
        claim_spec: dict = {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": size}},
        }
        storage_class = spec.get("storage_class") or settings.kubernetes_storage_class
        if storage_class:
            claim_spec["storageClassName"] = storage_class
        return [{"metadata": {"name": "workspace"}, "spec": claim_spec}]

    def _build_sandbox(
        self, user_id: str, policy_id: str, spec: dict, api_key: str
    ) -> dict:
        body: dict = {
            "apiVersion": f"{self._group}/{self._version}",
            "kind": "Sandbox",
            "metadata": {
                "name": _sandbox_name(user_id, policy_id),
                "namespace": self._ns,
                "labels": _labels(user_id, policy_id),
                "annotations": {
                    _ANN_USER_ID: user_id,
                    _ANN_POLICY_ID: policy_id,
                },
            },
            "spec": {
                "service": True,  # headless Service → stable serviceFQDN
                "podTemplate": self._build_pod_template(spec, api_key),
            },
        }
        vct = self._build_volume_claim_templates(spec)
        if vct:
            body["spec"]["volumeClaimTemplates"] = vct
        return body

    # ------------------------------------------------------------------
    # Custom-object CRUD
    # ------------------------------------------------------------------

    async def _get_sandbox(self, name: str) -> Optional[dict]:
        custom = await self._custom()
        try:
            return await custom.get_namespaced_custom_object(
                group=self._group,
                version=self._version,
                namespace=self._ns,
                plural=_PLURAL_SANDBOX,
                name=name,
            )
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return None
            raise

    async def _create_sandbox(self, body: dict) -> Optional[dict]:
        custom = await self._custom()
        try:
            return await custom.create_namespaced_custom_object(
                group=self._group,
                version=self._version,
                namespace=self._ns,
                plural=_PLURAL_SANDBOX,
                body=body,
            )
        except client.exceptions.ApiException as e:
            if e.status == 409:
                return await self._get_sandbox(body["metadata"]["name"])
            raise

    async def _set_operating_mode(self, name: str, mode: str) -> None:
        """Patch ``spec.operatingMode`` ("Running" or "Suspended")."""
        custom = await self._custom()
        try:
            await custom.patch_namespaced_custom_object(
                group=self._group,
                version=self._version,
                namespace=self._ns,
                plural=_PLURAL_SANDBOX,
                name=name,
                body={"spec": {"operatingMode": mode}},
                _content_type="application/merge-patch+json",
            )
            log.info("Set Sandbox %s operatingMode=%s", name, mode)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise

    # ------------------------------------------------------------------
    # Status / connection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_suspended(sandbox: dict) -> bool:
        """True when the Sandbox's desired state is Suspended."""
        return (sandbox.get("spec") or {}).get("operatingMode", "Running") == "Suspended"

    @staticmethod
    def _sandbox_running(sandbox: dict) -> bool:
        """True when the Sandbox desires Running and is *freshly* Ready.

        The controller's ``Ready`` condition is stale immediately after a spec
        change (e.g. resuming from a suspend): it still reads ``True`` from before
        the pod was torn down.  We guard against that by requiring the condition's
        ``observedGeneration`` to have caught up to ``metadata.generation`` — i.e.
        the controller has reconciled the *current* spec — so we never report a
        not-yet-recreated pod as ready.
        """
        if KubernetesSandboxBackend._is_suspended(sandbox):
            return False
        generation = (sandbox.get("metadata") or {}).get("generation")
        status = sandbox.get("status") or {}
        for c in status.get("conditions", []):
            if c.get("type") == "Ready":
                if c.get("status") != "True":
                    return False
                obs = c.get("observedGeneration")
                if generation is not None and obs is not None and obs < generation:
                    return False  # stale — controller hasn't reconciled new spec
                return True
        return False

    @staticmethod
    def _api_key_from_sandbox(sandbox: dict) -> Optional[str]:
        containers = (
            ((sandbox.get("spec") or {}).get("podTemplate") or {}).get("spec") or {}
        ).get("containers") or []
        for c in containers:
            for ev in c.get("env") or []:
                if ev.get("name") == _API_KEY_ENV:
                    return ev.get("value")
        return None

    def _connection_info(self, sandbox: dict) -> Optional[dict]:
        """Build the connection dict from a (ready) Sandbox, or None."""
        status = sandbox.get("status") or {}
        host = status.get("serviceFQDN")
        if not host:
            pod_ips = status.get("podIPs") or []
            host = pod_ips[0] if pod_ips else None
        if not host:
            return None
        api_key = self._api_key_from_sandbox(sandbox)
        if not api_key:
            return None
        name = sandbox["metadata"]["name"]
        return {
            "instance_id": name,
            "instance_name": name,
            "api_key": api_key,
            "host": host,
            "port": settings.sandbox_port,
        }

    async def _wait_until_ready(
        self, name: str, timeout: int = 120
    ) -> Optional[dict]:
        """Poll the Sandbox until it is Running with a connectable endpoint.

        Transient API errors (anything other than the 404 that ``_get_sandbox``
        maps to ``None``) are logged and retried until the deadline — a single
        API-server blip during the wait must not fail the whole provision.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                sandbox = await self._get_sandbox(name)
            except client.exceptions.ApiException as e:
                log.warning("Transient error polling Sandbox %s: %s", name, e)
                await asyncio.sleep(2)
                continue
            if sandbox is None:
                return None
            if self._sandbox_running(sandbox):
                info = self._connection_info(sandbox)
                if info:
                    return info
            await asyncio.sleep(2)
        log.warning("Sandbox %s not ready within %ds", name, timeout)
        return None

    # ------------------------------------------------------------------
    # Backend interface
    # ------------------------------------------------------------------

    async def provision(
        self,
        user_id: str,
        policy_id: str = "default",
        spec: Optional[dict] = None,
    ) -> Optional[dict]:
        """Create a Sandbox for the user+policy and wait until it is ready."""
        spec = spec or {}
        api_key = _generate_api_key()
        body = self._build_sandbox(user_id, policy_id, spec, api_key)
        await self._create_sandbox(body)
        return await self._wait_until_ready(_sandbox_name(user_id, policy_id))

    async def start(self, instance_id: str) -> bool:
        """Resume a suspended sandbox; idempotent if already running."""
        sandbox = await self._get_sandbox(instance_id)
        if sandbox is None:
            return False
        if self._is_suspended(sandbox):
            await self._set_operating_mode(instance_id, "Running")
        return True

    async def teardown(self, instance_id: str) -> None:
        """Delete the Sandbox (and its controller-managed Pod/Service/PVC)."""
        custom = await self._custom()
        try:
            await custom.delete_namespaced_custom_object(
                group=self._group,
                version=self._version,
                namespace=self._ns,
                plural=_PLURAL_SANDBOX,
                name=instance_id,
            )
            log.info("Deleted Sandbox %s", instance_id)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                log.warning("Could not delete Sandbox %s: %s", instance_id, e)

    async def _suspend(self, instance_id: str) -> None:
        """Suspend the Sandbox (scale-to-zero, identity + workspace preserved)."""
        await self._set_operating_mode(instance_id, "Suspended")

    async def status(self, instance_id: str) -> str:
        sandbox = await self._get_sandbox(instance_id)
        if sandbox is None:
            return "missing"
        return "running" if self._sandbox_running(sandbox) else "stopped"

    async def close(self) -> None:
        if self._api_client is not None:
            await self._api_client.close()
            self._api_client = None

    async def reconcile(self) -> None:
        """Rebuild in-memory tracking from live Sandboxes after a restart.

        Called once on startup (``main.py``).  The idle reaper only scans
        in-memory state, so without this any Sandbox left ``Running`` before a
        restart would never be suspended — it would keep running until a user
        happened to reconnect.  We list our managed Sandboxes, skip the ones
        already suspended, and re-track the rest so the reaper can suspend them
        when idle.  Per-policy idle timeouts aren't persisted, so adopted
        sandboxes fall back to ``settings.idle_timeout_minutes``.
        """
        custom = await self._custom()
        try:
            resp = await custom.list_namespaced_custom_object(
                group=self._group,
                version=self._version,
                namespace=self._ns,
                plural=_PLURAL_SANDBOX,
                label_selector=_MANAGED_BY,
            )
        except client.exceptions.ApiException as e:
            log.warning("reconcile: could not list Sandboxes: %s", e)
            return

        adopted = 0
        for sandbox in resp.get("items", []):
            if self._is_suspended(sandbox):
                continue  # already scaled to zero — nothing to reap
            ann = (sandbox.get("metadata") or {}).get("annotations") or {}
            user_id = ann.get(_ANN_USER_ID)
            if not user_id:
                continue  # not created by this backend / pre-annotation object
            info = self._connection_info(sandbox)
            if info is None:
                continue  # no endpoint yet — a later request will track it
            policy_id = ann.get(_ANN_POLICY_ID, "default")
            self._track(self._key(user_id, policy_id), info, None)
            adopted += 1

        if adopted:
            log.info(
                "reconcile: adopted %d running sandbox(es) for idle reaping",
                adopted,
            )

    # ------------------------------------------------------------------
    # Sandbox-aware ensure_terminal (get-or-create, resume if suspended)
    # ------------------------------------------------------------------

    async def ensure_terminal(
        self,
        user_id: str,
        policy_id: str = "default",
        spec: Optional[dict] = None,
    ) -> Optional[dict]:
        key = self._key(user_id, policy_id)
        name = _sandbox_name(user_id, policy_id)

        # Fast path — sandbox exists and is Running.
        sandbox = await self._get_sandbox(name)
        if sandbox is not None and self._sandbox_running(sandbox):
            info = self._connection_info(sandbox)
            if info:
                self._track(key, info, spec)
                return info

        if key not in self._locks:
            self._locks[key] = asyncio.Lock()

        async with self._locks[key]:
            sandbox = await self._get_sandbox(name)
            if sandbox is None:
                info = await self.provision(user_id, policy_id=policy_id, spec=spec)
                if info:
                    self._track(key, info, spec)
                return info

            # Exists — resume if suspended, then wait until ready.
            if self._is_suspended(sandbox):
                await self._set_operating_mode(name, "Running")
            info = await self._wait_until_ready(name)
            if info:
                self._track(key, info, spec)
            return info

    def _track(self, key: str, info: dict, spec: Optional[dict]) -> None:
        self._instances[key] = info
        self._specs[key] = spec or {}
        self._activity[key] = time.monotonic()

    async def get_terminal_info(self, user_id: str) -> Optional[dict]:
        sandbox = await self._get_sandbox(_sandbox_name(user_id))
        if sandbox is None or not self._sandbox_running(sandbox):
            return None
        return self._connection_info(sandbox)

    async def touch_activity(
        self, user_id: str, policy_id: str = "default"
    ) -> None:
        # Activity is tracked in-memory; the idle reaper suspends idle sandboxes.
        self._activity[self._key(user_id, policy_id)] = time.monotonic()

    # ------------------------------------------------------------------
    # Idle reaper — suspend (operatingMode Suspended) instead of tear down
    # ------------------------------------------------------------------

    async def _reap_idle(self) -> None:
        """Suspend (not delete) sandboxes idle past their timeout."""
        now = time.monotonic()
        for key in list(self._instances):
            info = self._instances.get(key)
            if info is None:
                continue
            spec = self._specs.get(key, {})
            timeout_min = spec.get(
                "idle_timeout_minutes", settings.idle_timeout_minutes
            )
            if not timeout_min or timeout_min <= 0:
                continue
            if now - self._activity.get(key, now) < timeout_min * 60:
                continue

            log.info("Suspending idle sandbox %s (key=%s)", info.get("instance_name"), key)
            try:
                await self._suspend(info["instance_id"])
            except Exception:
                log.exception("Failed to suspend %s", key)
            # Drop from active tracking; a returning request will resume it.
            self._instances.pop(key, None)
            self._specs.pop(key, None)
            self._activity.pop(key, None)
            self._locks.pop(key, None)
