from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "股市 AI 生態系 MVP"
    app_env: str = "development"
    data_catalog_path: str = "config/data_catalog.yaml"
    realtime_quote_provider: str = "twse_mis"
    fugle_marketdata_api_key: str | None = None
    realtime_stream_channels: str = "trades,books,candles"
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    line_access_token: str | None = None
    line_target_id: str | None = None
    # Headless n8n public API.  The API key remains opt-in and is never stored
    # in source control; without it the backend reports not activation-ready.
    n8n_automation_gateway_url: str | None = "http://127.0.0.1:5678"
    n8n_automation_gateway_token: str | None = None
    # Independent callback-only secret. It is rotated separately from the
    # n8n public API key and is never sent to n8n's management API.
    n8n_automation_callback_secret: str | None = None
    n8n_automation_gateway_timeout_seconds: float = 15.0
    n8n_automation_deployment_mode: str = "local"
    n8n_automation_remote_host_allowlist: str = ""
    n8n_automation_remote_tls_certificate_sha256: str | None = None
    n8n_automation_remote_mtls_certificate_path: str | None = None
    n8n_automation_remote_mtls_key_path: str | None = None
    n8n_automation_remote_tls_ca_bundle_path: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    @property
    def catalog_file(self) -> Path:
        path = Path(self.data_catalog_path)
        if not path.is_absolute():
            path = self.project_root / path
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()
