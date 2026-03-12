"""Backend factory — returns the configured Backend implementation."""

from terminals.backends.base import Backend
from terminals.config import settings


def create_backend() -> Backend:
    """Instantiate the backend selected by ``TERMINALS_BACKEND``."""
    if settings.backend == "docker":
        from terminals.backends.docker import DockerBackend

        return DockerBackend()
    elif settings.backend == "kubernetes":
        from terminals.backends.kubernetes import KubernetesBackend

        return KubernetesBackend()
    elif settings.backend == "kubernetes-operator":
        from terminals.backends.kubernetes_operator import KubernetesOperatorBackend

        return KubernetesOperatorBackend()
    raise ValueError(f"Unknown backend: {settings.backend!r}")
