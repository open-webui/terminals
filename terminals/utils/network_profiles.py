"""Resolve administrator-defined networking configuration for a policy."""

from terminals.backends.base import ProvisioningError
from terminals.config import settings


def resolve_network_profile(spec: dict | None, backend: str) -> dict:
    name = (spec or {}).get("network_profile")
    if name is None:
        return {}
    if not isinstance(name, str) or not name or name not in settings.network_profiles:
        raise ProvisioningError(f"Unknown network profile: {name!r}")

    profile = settings.network_profiles[name]
    if backend == "docker":
        network = profile.get("docker_network")
        if not isinstance(network, str) or not network.strip():
            raise ProvisioningError(f"Network profile {name!r} requires docker_network")
        networks = [item.strip() for item in network.split(",")]
        if any(not item or item in {"bridge", "default", "host", "none"} or item.startswith("container:") for item in networks):
            raise ProvisioningError(f"Network profile {name!r} requires named Docker networks")
    elif backend in ("kubernetes", "kubernetes-operator"):
        labels = profile.get("kubernetes_labels")
        if not isinstance(labels, dict) or not labels or any(
            not isinstance(key, str) or not key or not isinstance(value, str)
            for key, value in labels.items()
        ):
            raise ProvisioningError(f"Network profile {name!r} requires kubernetes_labels")
        if any(key.startswith(("openwebui.com/", "app.kubernetes.io/")) for key in labels):
            raise ProvisioningError(f"Network profile {name!r} cannot override managed labels")
    else:
        raise ProvisioningError(f"Network profiles are not supported by backend {backend!r}")
    return profile
