"""Application settings loaded from environment variables."""

from typing import Any
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Root data directory lives next to the *package* directory so that the
# location is the same regardless of the caller's working directory.
_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_DATA_DIR = str(_PACKAGE_DIR.parent / "data")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MACHINES_", env_file=".env")

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        # Resolve aliases within each source so process variables still override .env.
        for source in (env_settings, dotenv_settings):
            values = dict(source.env_vars)
            for name in cls.model_fields:
                suffixes = [name]
                if name in ("docker_network", "docker_data_dir"):
                    suffixes.append(name.removeprefix("docker_"))
                aliases = [prefix + suffix for prefix in ("MACHINES_", "TERMINALS_") for suffix in suffixes]
                if not source.case_sensitive:
                    aliases = [alias.lower() for alias in aliases]
                primary, *fallbacks = aliases
                for alias in fallbacks:
                    if alias in values:
                        value = values.pop(alias)
                        values.setdefault(primary, value)
            source.env_vars = values
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    api_key: str = ""

    # Database
    database_url: str = f"sqlite+aiosqlite:///{_DEFAULT_DATA_DIR}/terminals.db"

    # Backend selection: "docker", "kubernetes", or "kubernetes-operator"
    backend: str = "docker"
    network_profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)

    # Docker settings
    image: str = "ghcr.io/open-webui/open-terminal:latest"
    # Always, IfNotPresent, or Never; empty pulls :latest (or untagged) images on every
    # start and other tags only when missing. Applies to every backend.
    image_pull_policy: str = ""
    # Docker only: how long a pulled image is trusted before Always checks the registry again.
    image_pull_interval_seconds: int = 3600

    @field_validator("image_pull_policy")
    @classmethod
    def _validate_image_pull_policy(cls, value: str) -> str:
        from machines.utils.image_pull import normalize_policy

        return normalize_policy(value)
    docker_network: str = Field(
        default="",
        validation_alias=AliasChoices("MACHINES_DOCKER_NETWORK", "TERMINALS_DOCKER_NETWORK", "TERMINALS_NETWORK"),
    )
    docker_host: str = "127.0.0.1"  # address to reach published container ports
    # Leave room in a 63-character DNS label for "-" and the 32-character instance ID.
    docker_container_name_prefix: str = Field(default="terminal", pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,29}$")
    docker_mounts: str = ""          # JSON array of extra Docker bind mounts
    docker_data_dir: str = Field(
        default=f"{_DEFAULT_DATA_DIR}/terminals",
        validation_alias=AliasChoices("MACHINES_DOCKER_DATA_DIR", "TERMINALS_DOCKER_DATA_DIR", "TERMINALS_DATA_DIR"),
    )
    docker_host_data_dir: str = ""  # optional Docker-host path for docker_data_dir

    @field_validator("docker_host_data_dir")
    @classmethod
    def _validate_docker_host_data_dir(cls, value: str) -> str:
        if value and not Path(value).is_absolute():
            raise ValueError("MACHINES_DOCKER_HOST_DATA_DIR must be absolute")
        return value

    port: int = 3000
    host: str = "0.0.0.0"
    workers: int = 1
    log_level: str = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL
    enable_ui: bool = True
    status_cache_ttl: int = 30
    ws_compression: bool = False
    access_log: bool = False
    replay_body_limit: int | None = None
    proxy_connect_timeout_seconds: float = 10
    proxy_read_timeout_seconds: float = 360
    proxy_write_timeout_seconds: float = 300
    proxy_pool_timeout_seconds: float = 300
    provision_timeout_seconds: float = 120

    @field_validator("replay_body_limit", mode="before")
    @classmethod
    def _parse_replay_body_limit(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip().lower() in {
            "",
            "none",
            "null",
            "unlimited",
        }:
            return None
        return value

    # Kubernetes settings
    kubernetes_namespace: str = "terminals"
    kubernetes_image: str = ""                    # optional override; falls back to image
    kubernetes_storage_class: str = ""        # empty = cluster default
    kubernetes_storage_size: str = "1Gi"
    kubernetes_storage_mode: str = "per-user"  # per-user, shared, shared-rwo
    kubernetes_service_type: str = "ClusterIP"
    kubernetes_kubeconfig: str = ""           # empty = in-cluster config
    kubernetes_labels: str = ""               # extra labels as "k=v,k2=v2"
    kubernetes_restricted: bool = False        # OpenShift/restricted pod mode
    kubernetes_pod_security_context: dict[str, Any] = Field(default_factory=dict)
    kubernetes_container_security_context: dict[str, Any] = Field(default_factory=dict)
    kubernetes_node_selector: str = ""         # JSON object or "k=v,k2=v2"
    kubernetes_tolerations: str = ""           # JSON array of Kubernetes tolerations

    # Operator-specific settings
    kubernetes_crd_group: str = "openwebui.com"
    kubernetes_crd_version: str = "v1alpha1"

    # Idle reaper — tear down terminals after N minutes of inactivity (0 = disabled)
    idle_timeout_minutes: int = 0
    retention_period_minutes: int = Field(default=0, ge=0)
    idle_cleanup_timeout_seconds: float = 120
    # Hard cap — tear down terminals N minutes after they start, even if active (0 = disabled)
    max_lifetime_minutes: int = 0

    # Template hard caps (cannot be exceeded by API)
    max_cpu: str = ""              # MACHINES_MAX_CPU
    max_memory: str = ""           # MACHINES_MAX_MEMORY
    max_storage: str = ""          # MACHINES_MAX_STORAGE
    allowed_images: str = ""       # MACHINES_ALLOWED_IMAGES (comma-separated globs)

    @property
    def effective_kubernetes_image(self) -> str:
        return self.kubernetes_image or self.image


settings = Settings()
