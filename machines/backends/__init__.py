"""Backend factory — returns the configured Backend implementation."""

from machines.backends.base import Backend
from machines.config import settings


def create_backend() -> Backend:
    """Instantiate the backend selected by ``MACHINES_BACKEND``."""
    if settings.backend == "docker":
        from machines.backends.docker import DockerBackend

        return DockerBackend()
    elif settings.backend == "kubernetes":
        from machines.backends.kubernetes import KubernetesBackend

        return KubernetesBackend()
    elif settings.backend == "kubernetes-operator":
        from machines.backends.kubernetes_operator import KubernetesOperatorBackend

        return KubernetesOperatorBackend()
    raise ValueError(f"Unknown backend: {settings.backend!r}")
