"""Docker bind mount settings."""

import json
import posixpath
from pathlib import Path
from typing import Any


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


def parse_docker_mounts(raw: str) -> list[dict[str, Any]]:
    """Parse TERMINALS_DOCKER_MOUNTS into Docker HostConfig.Mounts entries."""
    raw = (raw or "").strip()
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("TERMINALS_DOCKER_MOUNTS must be valid JSON") from exc

    if not isinstance(data, list):
        raise ValueError("TERMINALS_DOCKER_MOUNTS must be a JSON array")

    mounts: list[dict[str, Any]] = []
    targets: list[str] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"TERMINALS_DOCKER_MOUNTS[{index}] must be an object")

        source = item.get("source")
        target = item.get("target")
        read_only = item.get("readOnly", True)

        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"TERMINALS_DOCKER_MOUNTS[{index}].source is required")
        if not isinstance(target, str) or not target.strip():
            raise ValueError(f"TERMINALS_DOCKER_MOUNTS[{index}].target is required")
        if not isinstance(read_only, bool):
            raise ValueError(f"TERMINALS_DOCKER_MOUNTS[{index}].readOnly must be a boolean")

        source = str(Path(source).expanduser())
        target = posixpath.normpath(target)

        if not Path(source).is_absolute():
            raise ValueError(f"TERMINALS_DOCKER_MOUNTS[{index}].source must be absolute")
        if not target.startswith("/"):
            raise ValueError(f"TERMINALS_DOCKER_MOUNTS[{index}].target must be absolute")
        if any(_is_under(target, unsafe) for unsafe in _UNSAFE_TARGETS):
            raise ValueError(f"TERMINALS_DOCKER_MOUNTS[{index}].target is unsafe: {target}")
        if any(_is_under(target, other) or _is_under(other, target) for other in targets):
            raise ValueError(f"TERMINALS_DOCKER_MOUNTS[{index}].target overlaps another mount")

        targets.append(target)
        mounts.append(
            {
                "Type": "bind",
                "Source": source,
                "Target": target,
                "ReadOnly": read_only,
            }
        )

    return mounts
