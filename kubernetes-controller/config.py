"""Configuration for the Terminals orchestrator loaded from environment variables."""

import os

# Kubernetes namespace where Terminal CRDs and pods are created.
TERMINAL_NAMESPACE: str = os.environ.get("TERMINAL_NAMESPACE", "open-webui")

# Default image for spawned Open Terminal containers.
DEFAULT_IMAGE: str = os.environ.get(
    "TERMINAL_DEFAULT_IMAGE", "ghcr.io/open-webui/open-terminal:latest"
)

# Default resource requests/limits for terminal pods.
DEFAULT_CPU_REQUEST: str = os.environ.get("TERMINAL_DEFAULT_CPU_REQUEST", "100m")
DEFAULT_MEMORY_REQUEST: str = os.environ.get("TERMINAL_DEFAULT_MEMORY_REQUEST", "256Mi")
DEFAULT_CPU_LIMIT: str = os.environ.get("TERMINAL_DEFAULT_CPU_LIMIT", "1")
DEFAULT_MEMORY_LIMIT: str = os.environ.get("TERMINAL_DEFAULT_MEMORY_LIMIT", "1Gi")

# Idle timeout in minutes before a terminal pod is culled.
DEFAULT_IDLE_TIMEOUT_MINUTES: int = int(
    os.environ.get("TERMINAL_DEFAULT_IDLE_TIMEOUT", "30")
)

# Persistence defaults.
DEFAULT_PERSISTENCE_ENABLED: bool = (
    os.environ.get("TERMINAL_DEFAULT_PERSISTENCE_ENABLED", "true").lower() == "true"
)
DEFAULT_PERSISTENCE_SIZE: str = os.environ.get("TERMINAL_DEFAULT_PERSISTENCE_SIZE", "1Gi")
DEFAULT_STORAGE_CLASS: str = os.environ.get("TERMINAL_DEFAULT_STORAGE_CLASS", "")

# API key for authenticating requests from Open WebUI to the orchestrator.
# When deployed via Helm, this is set from a Secret.
API_KEY: str = os.environ.get("TERMINALS_API_KEY", "")

# How long to wait (seconds) for a terminal pod to become ready before timing out.
PROVISION_TIMEOUT_SECONDS: int = int(
    os.environ.get("TERMINAL_PROVISION_TIMEOUT", "120")
)

# Host and port for the orchestrator server.
HOST: str = os.environ.get("TERMINALS_HOST", "0.0.0.0")
PORT: int = int(os.environ.get("TERMINALS_PORT", "8080"))
