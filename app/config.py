"""Configuration. Every knob is an environment variable; nothing is read from a file."""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHELFMARK_")

    data_dir: Path = Path("./data")
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "shelfmark.db"


settings = Settings()
