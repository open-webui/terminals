"""Environment helpers for Open Terminal instances."""

import os
from collections.abc import Mapping
from typing import Any

from terminals.utils.parsing import parse_cpu_nanos, parse_memory

_DEFAULT_OPEN_TERMINAL_ENV = ("OPEN_TERMINAL_SYSTEM_PROMPT",)
_RESERVED_CONTAINER_ENV = {"OPEN_TERMINAL_API_KEY"}


def _format_cpu_count(value: str) -> str:
    cores = parse_cpu_nanos(value) / 1_000_000_000
    if cores.is_integer():
        return str(int(cores))
    return f"{cores:.3f}".rstrip("0").rstrip(".")


def build_terminal_env(
    policy_env: Mapping[str, Any] | None = None,
    *,
    cpu_limit: str | None = None,
    memory_limit: str | None = None,
) -> dict[str, str]:
    """Return env vars that should be passed into an Open Terminal container."""
    env: dict[str, str] = {}

    for key in _DEFAULT_OPEN_TERMINAL_ENV:
        value = os.environ.get(key)
        if value:
            env[key] = value

    for key, value in (policy_env or {}).items():
        key = str(key)
        if key in _RESERVED_CONTAINER_ENV or value is None:
            continue
        env[key] = str(value)

    if cpu_limit:
        cpu_limit = str(cpu_limit)
        env["OPEN_TERMINAL_CPU_LIMIT"] = cpu_limit
        env["OPEN_TERMINAL_CPU_COUNT"] = _format_cpu_count(cpu_limit)

    if memory_limit:
        memory_limit = str(memory_limit)
        env["OPEN_TERMINAL_MEMORY_LIMIT"] = memory_limit
        env["OPEN_TERMINAL_MEMORY_BYTES"] = str(parse_memory(memory_limit))

    return env
