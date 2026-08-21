from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TransportName(StrEnum):
    DIRECT = "direct"
    BYEDPI = "byedpi"
    PROXY = "proxy"
    EXTERNAL_SOCKS = "external_socks"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_db: str = "lead_radar"
    postgres_user: str = "lead_radar"
    postgres_password: SecretStr = SecretStr("")
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    telegram_api_id: int | None = None
    telegram_api_hash: SecretStr = SecretStr("")
    telegram_notify_bot_token: SecretStr = SecretStr("")
    telegram_notify_chat_id: str | None = None
    telegram_transport: TransportName = TransportName.DIRECT
    telegram_bot_transport: TransportName = TransportName.DIRECT
    # Preferred explicit proxy URL.  This retains the hostname so SOCKS5h can
    # resolve destinations through the tunnel rather than through Docker DNS.
    telegram_proxy_url: str = ""
    telegram_proxy_type: str = "socks5"
    telegram_proxy_host: str = ""
    telegram_proxy_port: int = Field(default=1080, ge=1, le=65535)
    telegram_proxy_username: str = ""
    telegram_proxy_password: SecretStr = SecretStr("")
    forum_transport: TransportName = TransportName.DIRECT
    discord_user_token: SecretStr = SecretStr("")
    discord_transport: TransportName = TransportName.DIRECT
    reddit_transport: TransportName = TransportName.DIRECT
    reddit_poll_min_seconds: int = Field(default=60, ge=30, le=900)
    reddit_poll_max_seconds: int = Field(default=120, ge=30, le=900)

    yandex_api_key: SecretStr = Field(default=SecretStr(""), validation_alias=AliasChoices("YANDEX_API_KEY", "YANDEX_CLOUD_API_KEY"))
    yandex_folder_id: str = ""
    yandex_model_uri: str = Field(default="", validation_alias=AliasChoices("YANDEX_MODEL_URI", "YANDEX_CHAT_MODEL_URI"))
    yandex_openai_base_url: str = Field(default="", validation_alias=AliasChoices("YANDEX_OPENAI_BASE_URL"))
    yandex_transport: TransportName = TransportName.DIRECT
    lead_alert_threshold: int = Field(default=72, ge=0, le=100)
    rejected_audit_rate: float = Field(default=0.01, ge=0, le=1)
    rejected_audit_daily_limit: int = Field(default=15, ge=0, le=20)

    byedpi_proxy: str = "socks5://byedpi:1080"
    external_proxy: str = ""
    backfill_limit: int = Field(default=30, ge=1, le=100)
    forum_poll_min_seconds: int = Field(default=45, ge=15, le=600)
    forum_poll_max_seconds: int = Field(default=90, ge=15, le=600)
    sources_path: Path = Path("config/sources.yaml")
    profile_path: Path = Path("config/profile.yaml")
    pricing_floor_micro: int = Field(default=50, ge=0)
    pricing_floor_small: int = Field(default=100, ge=0)
    pricing_floor_medium: int = Field(default=300, ge=0)
    pricing_floor_large: int = Field(default=800, ge=0)
    telegram_session_path: Path = Path("/data/telegram/radar")
    auth_secret_key: SecretStr = SecretStr("")
    my_internal_id: str = ""
    local_gateway_host: str = "0.0.0.0"
    local_gateway_port: int = Field(default=8765, ge=1, le=65535)
    local_gateway_client_host: str = "127.0.0.1"
    local_gateway_sqlite_path: Path = Path("/data/gateway/seen_messages.sqlite3")

    @field_validator("external_proxy")
    @classmethod
    def external_proxy_required_for_external_transport(cls, value: str) -> str:
        return value.strip()

    @property
    def database_url(self) -> str:
        password = self.postgres_password.get_secret_value()
        return f"postgresql+asyncpg://{self.postgres_user}:{password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    def configured(self, secret: SecretStr | str | None) -> bool:
        return bool(secret.get_secret_value().strip() if isinstance(secret, SecretStr) else secret)

    @property
    def resolved_yandex_model_uri(self) -> str:
        """Accept the full URI, or repair the common missing-folder shorthand."""
        value = self.yandex_model_uri.strip() or "qwen3.6-35b-a3b"
        # A valid Qwen URI is gpt://<folder_id>/qwen3.6-35b-a3b.  Older
        # examples sometimes led users to put gpt://qwen.../latest here.
        if value.startswith("gpt://qwen"):
            value = value.removeprefix("gpt://")
            if value.endswith("/latest"):
                value = value.removesuffix("/latest")
            return f"gpt://{self.yandex_folder_id}/{value}"
        if not value.startswith("gpt://"):
            return f"gpt://{self.yandex_folder_id}/{value.removesuffix('/latest')}"
        return value

    @property
    def yandex_uses_openai_compat(self) -> bool:
        return bool(self.yandex_openai_base_url.strip())

    @property
    def active_yandex_model_uri(self) -> str:
        # The OpenAI-compatible endpoint accepts the user's original complete
        # URI, including /latest.  The native Completion endpoint does not.
        return self.yandex_model_uri.strip() if self.yandex_uses_openai_compat else self.resolved_yandex_model_uri


@lru_cache
def get_settings() -> Settings:
    return Settings()
