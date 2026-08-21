from __future__ import annotations

from app.config import Settings, TransportName
from urllib.parse import quote


class TransportConfigurationError(ValueError):
    pass


def proxy_url(settings: Settings, transport: TransportName) -> str | None:
    """Resolve a proxy only for the calling adapter; never change global env."""
    if transport is TransportName.DIRECT:
        return None
    if transport is TransportName.BYEDPI:
        return settings.byedpi_proxy
    if transport is TransportName.PROXY:
        if settings.telegram_proxy_url:
            return settings.telegram_proxy_url
        if not settings.telegram_proxy_host:
            raise TransportConfigurationError("TELEGRAM_PROXY_HOST is required for proxy transport")
        auth = ""
        if settings.telegram_proxy_username:
            auth = f"{quote(settings.telegram_proxy_username)}:{quote(settings.telegram_proxy_password.get_secret_value())}@"
        return f"{settings.telegram_proxy_type}://{auth}{settings.telegram_proxy_host}:{settings.telegram_proxy_port}"
    if transport is TransportName.EXTERNAL_SOCKS and settings.external_proxy:
        return settings.external_proxy
    raise TransportConfigurationError("EXTERNAL_PROXY is required for external_socks transport")
