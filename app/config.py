"""Configuration. Every knob is an environment variable; nothing is read from a file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHELFMARK_")

    data_dir: Path = Path("./data")
    host: str = "0.0.0.0"
    # Named listen_port, not port: Kubernetes injects SHELFMARK_PORT=tcp://...
    # for a Service called "shelfmark", and a settings field called port would
    # try to parse that as an integer and crash the process at import.
    listen_port: int = 8080
    log_level: str = "info"
    ipn_prefix: str = "SK"
    # Secret used to sign the smk_session cookie. The container is expected to
    # provide a real value in production; this default keeps the dev loop working.
    secret_key: str = "dev-insecure-secret-key"
    # "none" disables auth enforcement; refused when env is "production".
    auth: str = "on"
    env: str = "development"
    # Per-key requests per minute (0 disables). Unauthenticated clients get 1/10.
    rate_limit: int = 600
    # First admin key on an empty api_key table; generated when unset.
    bootstrap_key: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "shelfmark.db"


settings = Settings()
