"""Docker backend — provisions Open Terminal inside containers via aiodocker."""

import asyncio
import hashlib
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Optional

import aiodocker
import httpx

from terminals.backends.base import Backend
from terminals.config import settings
from terminals.utils.parsing import parse_cpu_nanos, parse_memory

log = logging.getLogger(__name__)

# Container name prefix used for discovery during reconciliation.
_CONTAINER_PREFIX = "terminals-"
_DNS_SAFE = re.compile(r"[^a-z0-9-]")


class DockerBackend(Backend):
    """Manage terminal instances as Docker containers."""

    def __init__(self) -> None:
        super().__init__()
        self._docker: Optional[aiodocker.Docker] = None

    async def _get_docker(self) -> aiodocker.Docker:
        if self._docker is None:
            self._docker = aiodocker.Docker()
        return self._docker

    @staticmethod
    def _container_name(policy_id: str, user_id: str) -> str:
        """Build a deterministic, DNS-safe container name (≤63 chars)."""
        short = hashlib.sha256(user_id.encode()).hexdigest()[:12]
        if policy_id == "default":
            return f"{_CONTAINER_PREFIX}{short}"
        policy_slug = _DNS_SAFE.sub("-", policy_id.lower()).strip("-")[:20]
        return f"{_CONTAINER_PREFIX}{short}-{policy_slug}"

    # ------------------------------------------------------------------
    # Backend interface
    # ------------------------------------------------------------------

    async def provision(
        self,
        user_id: str,
        policy_id: str = "default",
        spec: dict | None = None,
    ) -> dict:
        docker = await self._get_docker()
        api_key = secrets.token_urlsafe(24)
        instance_name = self._container_name(policy_id, user_id)
        host_data_dir = str((Path(settings.data_dir) / user_id).resolve())
        s = spec or {}

        image = s.get("image", settings.image)

        host_config: dict = {
            "Binds": [f"{host_data_dir}:/home/user"],
            "PublishAllPorts": True,
        }

        # Resources
        if s.get("memory_limit"):
            host_config["Memory"] = parse_memory(s["memory_limit"])
        if s.get("cpu_limit"):
            host_config["NanoCpus"] = parse_cpu_nanos(s["cpu_limit"])

        # Egress filtering is handled inside the container (dnsmasq + ipset +
        # iptables + capsh) triggered by OPEN_TERMINAL_ALLOWED_DOMAINS env var.
        # Grant CAP_NET_ADMIN so the entrypoint can set up iptables rules
        # (the capability gets permanently dropped via capsh after setup).
        policy_env = s.get("env", {})
        if "OPEN_TERMINAL_ALLOWED_DOMAINS" in policy_env:
            host_config["CapAdd"] = ["NET_ADMIN"]
        if settings.network:
            host_config["NetworkMode"] = settings.network

        # Env vars
        env = [f"OPEN_TERMINAL_API_KEY={api_key}"]
        for k, v in policy_env.items():
            env.append(f"{k}={v}")

        config: dict = {
            "Image": image,
            "Env": env,
            "HostConfig": host_config,
            "ExposedPorts": {"8000/tcp": {}},
            "Labels": {
                "app.kubernetes.io/managed-by": "terminals",
                "openwebui.com/user-id": user_id,
                "openwebui.com/policy": policy_id,
            },
        }

        log.info("Provisioning container %s for user %s (policy=%s)", instance_name, user_id, policy_id)

        max_conflict_retries = 3
        for attempt in range(max_conflict_retries + 1):
            try:
                container = await docker.containers.create_or_replace(
                    name=instance_name,
                    config=config,
                )
                await container.start()
                break
            except aiodocker.exceptions.DockerError as exc:
                if exc.status == 409 and attempt < max_conflict_retries:
                    log.warning(
                        "Container %s conflict (attempt %d/%d), force-removing and retrying",
                        instance_name, attempt + 1, max_conflict_retries,
                    )
                    try:
                        old = await docker.containers.get(instance_name)
                        await old.delete(force=True)
                    except aiodocker.exceptions.DockerError:
                        pass
                    await asyncio.sleep(1)
                    continue
                log.error("Failed to provision container for %s: %s", user_id, exc)
                raise

        result = await self._extract_instance_info(container, instance_name, api_key)
        await self._wait_until_ready(result, timeout=15)
        return result

    async def _wait_until_ready(self, instance: dict, timeout: int = 15) -> None:
        """Poll the container's /health endpoint until it responds."""

        url = f"http://{instance['host']}:{instance['port']}/health"
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=3) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        log.info("Container %s is ready", instance["instance_name"])
                        return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        log.warning("Container %s did not become ready within %ds", instance["instance_name"], timeout)

    async def _extract_instance_info(
        self,
        container,
        instance_name: str,
        api_key: str,
    ) -> dict:
        """Read container metadata and return the instance info dict."""
        info = await container.show()
        instance_id = info["Id"]

        # When using a custom Docker network, containers can reach each other
        # by name.  Otherwise, use the published port on the Docker host.
        if settings.network:
            host = instance_name
            port = 8000
        else:
            port_bindings = (
                info.get("NetworkSettings", {})
                .get("Ports", {})
                .get("8000/tcp", [])
            )
            if port_bindings:
                port = int(port_bindings[0]["HostPort"])
            else:
                port = 8000
            host = settings.docker_host

        return {
            "instance_id": instance_id,
            "instance_name": instance_name,
            "api_key": api_key,
            "host": host,
            "port": port,
        }

    # ------------------------------------------------------------------
    # Reconciliation — rediscover running containers on startup
    # ------------------------------------------------------------------

    async def reconcile(self) -> None:
        """Scan running Docker containers and repopulate ``_instances``.

        Called during startup to recover state after a restart without
        tearing down existing containers.  Uses Docker labels to identify
        user_id and policy_id.  The API key is read from the container's
        ``OPEN_TERMINAL_API_KEY`` env var.
        """
        docker = await self._get_docker()
        containers = await docker.containers.list(
            filters={
                "label": ["app.kubernetes.io/managed-by=terminals"],
                "status": ["running"],
            },
        )

        recovered = 0
        for container in containers:
            info = await container.show()
            name = info.get("Name", "").lstrip("/")
            labels = info.get("Config", {}).get("Labels", {})

            user_id = labels.get("openwebui.com/user-id")
            policy_id = labels.get("openwebui.com/policy", "default")
            if not user_id:
                log.debug("Skipping container %s: no user-id label", name)
                continue

            key = self._key(user_id, policy_id)

            # Already tracked
            if key in self._instances:
                continue

            # Extract API key from container env
            env_list = info.get("Config", {}).get("Env", [])
            api_key = ""
            for entry in env_list:
                if entry.startswith("OPEN_TERMINAL_API_KEY="):
                    api_key = entry.split("=", 1)[1]
                    break

            instance_info = await self._extract_instance_info(container, name, api_key)
            self._instances[key] = instance_info
            self._activity[key] = time.monotonic()
            recovered += 1
            log.info("Reconciled container %s → %s:%s", name, instance_info["host"], instance_info["port"])

        if recovered:
            log.info("Reconciled %d running container(s)", recovered)

    # ------------------------------------------------------------------
    # Container lifecycle
    # ------------------------------------------------------------------

    async def start(self, instance_id: str) -> bool:
        current = await self.status(instance_id)
        if current == "running":
            return True
        if current == "stopped":
            docker = await self._get_docker()
            try:
                container = await docker.containers.get(instance_id)
                await container.start()
                return True
            except aiodocker.exceptions.DockerError as exc:
                log.error("Failed to restart container %s: %s", instance_id, exc)
                return False
        return False  # missing

    async def teardown(self, instance_id: str) -> None:
        docker = await self._get_docker()
        try:
            container = await docker.containers.get(instance_id)
            await container.stop(t=10)
        except aiodocker.exceptions.DockerError:
            pass
        try:
            container = await docker.containers.get(instance_id)
            await container.delete(force=True)
        except aiodocker.exceptions.DockerError:
            log.warning("Could not remove container %s (may already be gone)", instance_id)

    async def status(self, instance_id: str) -> str:
        docker = await self._get_docker()
        try:
            container = await docker.containers.get(instance_id)
            info = await container.show()
            state = info.get("State", {})
            if state.get("Running"):
                return "running"
            return "stopped"
        except aiodocker.exceptions.DockerError:
            return "missing"

    async def close(self) -> None:
        if self._docker is not None:
            await self._docker.close()
            self._docker = None
