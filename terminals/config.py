"""Application settings loaded from environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TERMINALS_", env_file=".env")

    api_key: str = ""
    open_webui_url: str = ""  # if set, validate JWTs against this Open WebUI instance

    # Backend selection: "docker", "local", or "static"
    backend: str = "docker"

    # Docker settings
    image: str = "ghcr.io/open-webui/open-terminal:latest"
    network: str = ""
    data_dir: str = "./data/terminals"

    # Local settings (process-managed)
    local_binary: str = "open-terminal"
    local_port_range_start: int = 9000

    # Static settings (pre-running instance)
    static_host: str = "127.0.0.1"
    static_port: int = 8000
    static_api_key: str = ""

    port: int = 3000
    host: str = "0.0.0.0"

    # Kubernetes settings (shared by kubernetes and kubernetes-operator backends)
    kubernetes_namespace: str = "terminals"
    kubernetes_image: str = "ghcr.io/open-webui/open-terminal:latest"
    kubernetes_storage_class: str = ""        # empty = cluster default
    kubernetes_storage_size: str = "1Gi"
    kubernetes_service_type: str = "ClusterIP"
    kubernetes_kubeconfig: str = ""           # empty = in-cluster config
    kubernetes_labels: str = ""               # extra labels as "k=v,k2=v2"

    # Operator-specific settings
    kubernetes_crd_group: str = "openwebui.com"
    kubernetes_crd_version: str = "v1alpha1"

    # Operator resource defaults (applied to Terminal CRs)
    kubernetes_default_cpu_request: str = "100m"
    kubernetes_default_cpu_limit: str = "1"
    kubernetes_default_memory_request: str = "256Mi"
    kubernetes_default_memory_limit: str = "1Gi"
    kubernetes_default_idle_timeout_minutes: int = 30
    kubernetes_default_persistence_enabled: bool = True
    kubernetes_default_persistence_size: str = "1Gi"

    # Lifecycle management
    idle_timeout_seconds: int = 1800   # 30 min — stop instances idle this long
    cleanup_interval_seconds: int = 60 # how often to check for idle instances

    # Audit logging
    siem_webhook_url: str = ""         # if set, forward audit events to this URL

    # Encryption
    encryption_key: str = ""           # if empty, auto-generated and persisted


settings = Settings()
