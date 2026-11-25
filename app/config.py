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

    @property
    def db_path(self) -> Path:
        return self.data_dir / "shelfmark.db"


settings = Settings()
