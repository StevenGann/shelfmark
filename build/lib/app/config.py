"""Configuration. Every knob is an environment variable; nothing is read from a file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHELFMARK_")

    data_dir: Path = Path("./data")
    # Local copies of datasheets and images live here, outside the database so
    # they survive on an NFS mount and are served without a database round trip.
    asset_dir: Path = Path("/data/assets")
    # DigiKey Product Information API v4 client credentials. When either is
    # empty, enrichment jobs stay queued and report "credentials not configured".
    digikey_client_id: str = ""
    digikey_client_secret: str = ""
    # Daily cap on DigiKey API calls (productdetails lookups, not asset downloads).
    digikey_daily_budget: int = 1000
    host: str = "0.0.0.0"
    # Named listen_port, not port: Kubernetes injects SHELFMARK_PORT=tcp://...
    # for a Service called "shelfmark", and a settings field called port would
    # try to parse that as an integer and crash the process at import.
    listen_port: int = 8080
    log_level: str = "info"
    ipn_prefix: str = "SK"
    # "none" disables auth enforcement; refused when env is "production".
    auth: str = "on"
    env: str = "development"
    # Per-key requests per minute (0 disables). Unauthenticated clients get 1/10.
    rate_limit: int = 600
    # First admin key on an empty api_key table; generated when unset.
    bootstrap_key: str = ""
    # How often the app drains the enrichment queue itself, in seconds.
    # 0 disables the timer (the CLI still works); the pod runs one process, so
    # the tick is the only scheduler there is.
    queue_tick_seconds: int = 900

    @property
    def db_path(self) -> Path:
        return self.data_dir / "shelfmark.db"


settings = Settings()
