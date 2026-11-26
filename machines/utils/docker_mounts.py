"""Docker bind mount settings."""

import hashlib
import json
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

from machines.utils.context import get_terminal_id


_UNSAFE_TARGETS = {
    "/",
    "/app",
    "/bin",
    "/dev",
    "/etc",
    "/home",
    "/home/user",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
    "/var",
}


def _is_under(path: str, parent: str) -> bool:
    if parent == "/":
        return path == "/"
    return path == parent or path.startswith(parent.rstrip("/") + "/")


@dataclass(frozen=True)
class DockerMount:
    source: str
    target: str
    read_only: bool
    isolated: bool

    def for_instance(self, template_id: str, context_id: str) -> dict:
        mount = {"Type": "bind", "Source": self.source, "Target": self.target, "ReadOnly": self.read_only}
        if self.isolated:
            if context_id.startswith("user:"):
                key = context_id.removeprefix("user:")
                # Keep user paths distinct on case-insensitive hosts and reserve terminal IDs.
                safe_name = re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,254}", key)
                if not safe_name or re.fullmatch(r"[0-9a-f]{32}", key):
                    key = "~" + hashlib.sha256(key.encode()).hexdigest()
            else:
                key = get_terminal_id(template_id, context_id)
            mount["Source"] = posixpath.join(self.source, key)
            mount["BindOptions"] = {"CreateMountpoint": True}
        return mount


def parse_docker_mounts(raw: str) -> list[DockerMount]:
    """Validate the extra Docker bind mounts at startup."""
    raw = (raw or "").strip()
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("MACHINES_DOCKER_MOUNTS must be valid JSON") from exc

    if not isinstance(data, list):
        raise ValueError("MACHINES_DOCKER_MOUNTS must be a JSON array")

    mounts: list[DockerMount] = []
    targets: list[str] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"MACHINES_DOCKER_MOUNTS[{index}] must be an object")

        source = item.get("source")
        target = item.get("target")
        read_only = item.get("readOnly", True)
        isolated = item.get("isolated", False)

        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"MACHINES_DOCKER_MOUNTS[{index}].source is required")
        if not isinstance(target, str) or not target.strip():
            raise ValueError(f"MACHINES_DOCKER_MOUNTS[{index}].target is required")
        if not isinstance(read_only, bool):
            raise ValueError(f"MACHINES_DOCKER_MOUNTS[{index}].readOnly must be a boolean")
        if not isinstance(isolated, bool):
            raise ValueError(f"MACHINES_DOCKER_MOUNTS[{index}].isolated must be a boolean")

        source = str(Path(source).expanduser())
        target = posixpath.normpath(target)

        if not Path(source).is_absolute():
            raise ValueError(f"MACHINES_DOCKER_MOUNTS[{index}].source must be absolute")
        if not target.startswith("/"):
            raise ValueError(f"MACHINES_DOCKER_MOUNTS[{index}].target must be absolute")
        if any(_is_under(target, unsafe) for unsafe in _UNSAFE_TARGETS):
            raise ValueError(f"MACHINES_DOCKER_MOUNTS[{index}].target is unsafe: {target}")
        if any(_is_under(target, other) or _is_under(other, target) for other in targets):
            raise ValueError(f"MACHINES_DOCKER_MOUNTS[{index}].target overlaps another mount")

        targets.append(target)
        mounts.append(DockerMount(source, target, read_only, isolated))

    return mounts
