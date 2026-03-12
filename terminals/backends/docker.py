"""Docker backend — provisions Open Terminal inside containers via aiodocker."""

import logging
import secrets
from typing import Optional

import aiodocker

from terminals.backends.base import Backend
from terminals.config import settings

log = logging.getLogger(__name__)


class DockerBackend(Backend):
    """Manage terminal instances as Docker containers."""

    def __init__(self) -> None:
        self._docker: Optional[aiodocker.Docker] = None

    async def _get_docker(self) -> aiodocker.Docker:
        if self._docker is None:
            self._docker = aiodocker.Docker()
        return self._docker

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
        instance_name = f"terminals-{user_id}-{policy_id}"
        host_data_dir = f"{settings.data_dir}/{user_id}"
        s = spec or {}

        image = s.get("image", settings.image)

        host_config: dict = {
            "Binds": [f"{host_data_dir}:/home/user"],
        }

        # Resources
        if s.get("memory_limit"):
            host_config["Memory"] = self._parse_memory(s["memory_limit"])
        if s.get("cpu_limit"):
            host_config["NanoCpus"] = self._parse_cpu_nanos(s["cpu_limit"])

        # Network
        allowed = s.get("allowed_domains")
        if allowed is not None and len(allowed) == 0:
            host_config["NetworkMode"] = "none"
        elif settings.network:
            host_config["NetworkMode"] = settings.network

        # Env vars
        env = [f"OPEN_TERMINAL_API_KEY={api_key}"]
        for k, v in s.get("env", {}).items():
            env.append(f"{k}={v}")

        config: dict = {
            "Image": image,
            "Env": env,
            "HostConfig": host_config,
            "ExposedPorts": {"8000/tcp": {}},
        }

        log.info("Provisioning container %s for user %s (policy=%s)", instance_name, user_id, policy_id)

        try:
            container = await docker.containers.create_or_replace(
                name=instance_name,
                config=config,
            )
            await container.start()
        except aiodocker.exceptions.DockerError as exc:
            log.error("Failed to provision container for %s: %s", user_id, exc)
            raise

        info = await container.show()
        instance_id = info["Id"]

        host = instance_name
        if not settings.network and (allowed is None or len(allowed) > 0):
            networks = info.get("NetworkSettings", {}).get("Networks", {})
            bridge = networks.get("bridge", {})
            host = bridge.get("IPAddress", "127.0.0.1")

        return {
            "instance_id": instance_id,
            "instance_name": instance_name,
            "api_key": api_key,
            "host": host,
            "port": 8000,
        }

    # ------------------------------------------------------------------
    # Resource parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_memory(value: str) -> int:
        """Parse K8s memory string to bytes. '512Mi' -> 536870912."""
        import re
        m = re.match(r"^(\d+(?:\.\d+)?)\s*(Ki|Mi|Gi|Ti)?$", str(value).strip())
        if not m:
            return int(value)
        num, suffix = float(m.group(1)), m.group(2) or ""
        mult = {"": 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}
        return int(num * mult[suffix])

    @staticmethod
    def _parse_cpu_nanos(value: str) -> int:
        """Parse K8s CPU string to nanocpus. '2' -> 2_000_000_000, '500m' -> 500_000_000."""
        import re
        m = re.match(r"^(\d+(?:\.\d+)?)\s*(m)?$", str(value).strip())
        if not m:
            return int(float(value) * 1_000_000_000)
        num, suffix = float(m.group(1)), m.group(2) or ""
        if suffix == "m":
            return int(num * 1_000_000)
        return int(num * 1_000_000_000)

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
