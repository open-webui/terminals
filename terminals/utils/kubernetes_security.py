"""Kubernetes pod security helpers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from terminals.config import settings


RESTRICTED_POD_SECURITY_CONTEXT: dict[str, Any] = {
    "runAsNonRoot": True,
    "seccompProfile": {"type": "RuntimeDefault"},
}

RESTRICTED_CONTAINER_SECURITY_CONTEXT: dict[str, Any] = {
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
    "runAsNonRoot": True,
}

INCOMPATIBLE_RESTRICTED_ENV = {
    "OPEN_TERMINAL_ALLOWED_DOMAINS",
    "OPEN_TERMINAL_PACKAGES",
    "OPEN_TERMINAL_PIP_PACKAGES",
    "OPEN_TERMINAL_NPM_PACKAGES",
}


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def deep_merge(*items: Mapping[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        if not item:
            continue
        for key, value in item.items():
            if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
                result[key] = deep_merge(result[key], value)
            else:
                result[key] = deepcopy(value)
    return result


def restricted_enabled(spec: Mapping[str, Any] | None = None) -> bool:
    spec = spec or {}
    if "restricted" in spec:
        return truthy(spec.get("restricted"))
    return bool(settings.kubernetes_restricted)


def pod_security_context(spec: Mapping[str, Any] | None = None) -> dict[str, Any]:
    spec = spec or {}
    base = RESTRICTED_POD_SECURITY_CONTEXT if restricted_enabled(spec) else {}
    return deep_merge(
        base,
        settings.kubernetes_pod_security_context,
        spec.get("pod_security_context") or spec.get("podSecurityContext"),
    )


def container_security_context(spec: Mapping[str, Any] | None = None) -> dict[str, Any]:
    spec = spec or {}
    base = RESTRICTED_CONTAINER_SECURITY_CONTEXT if restricted_enabled(spec) else {}
    return deep_merge(
        base,
        settings.kubernetes_container_security_context,
        spec.get("container_security_context") or spec.get("containerSecurityContext"),
    )


def restricted_env_errors(env: Mapping[str, Any] | None) -> list[str]:
    env = env or {}
    errors = [
        f"{key} is not supported in restricted Kubernetes/OpenShift mode"
        for key in sorted(INCOMPATIBLE_RESTRICTED_ENV.intersection(env))
    ]
    multi_user = env.get("OPEN_TERMINAL_MULTI_USER")
    if truthy(multi_user):
        errors.append(
            "OPEN_TERMINAL_MULTI_USER=true is not supported in restricted "
            "Kubernetes/OpenShift mode"
        )
    return errors
