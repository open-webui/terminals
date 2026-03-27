"""Application settings loaded from environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Root data directory lives next to the *package* directory so that the
# location is the same regardless of the caller's working directory.
_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_DATA_DIR = str(_PACKAGE_DIR.parent / "data")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TERMINALS_", env_file=".env")

    api_key: str = ""
    open_webui_url: str = ""  # if set, validate JWTs against this Open WebUI instance

    # Database
    database_url: str = f"sqlite+aiosqlite:///{_DEFAULT_DATA_DIR}/terminals.db"

    # Backend selection: "docker", "kubernetes", or "kubernetes-operator"
    backend: str = "docker"

    # Docker settings
    image: str = "ghcr.io/open-webui/open-terminal:latest"
    network: str = ""
    docker_host: str = "127.0.0.1"  # address to reach published container ports
    data_dir: str = f"{_DEFAULT_DATA_DIR}/terminals"

    port: int = 3000
    host: str = "0.0.0.0"

    # Kubernetes settings
    kubernetes_namespace: str = "terminals"
    kubernetes_image: str = "ghcr.io/open-webui/open-terminal:latest"
    kubernetes_storage_class: str = ""        # empty = cluster default
    kubernetes_storage_size: str = "1Gi"
    kubernetes_storage_mode: str = "per-user"  # per-user, shared, shared-rwo
    kubernetes_service_type: str = "ClusterIP"
    kubernetes_kubeconfig: str = ""           # empty = in-cluster config
    kubernetes_labels: str = ""               # extra labels as "k=v,k2=v2"

    # Operator-specific settings
    kubernetes_crd_group: str = "openwebui.com"
    kubernetes_crd_version: str = "v1alpha1"

    # Idle reaper — tear down terminals after N minutes of inactivity (0 = disabled)
    idle_timeout_minutes: int = 0

    # Policy hard caps (cannot be exceeded by API)
    max_cpu: str = ""              # TERMINALS_MAX_CPU
    max_memory: str = ""           # TERMINALS_MAX_MEMORY
    max_storage: str = ""          # TERMINALS_MAX_STORAGE
    allowed_images: str = ""       # TERMINALS_ALLOWED_IMAGES (comma-separated globs)


settings = Settings()
