from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings


@dataclass(frozen=True)
class HealthState:
    postgres: bool
    byedpi: bool
    yandex: bool | None
    telegram: bool | None

    @property
    def ok(self) -> bool:
        return self.postgres and self.byedpi

    def as_dict(self) -> dict[str, bool | str | None]:
        return {"status": "ok" if self.ok else "degraded", "postgres": self.postgres,
                "yandex": self.yandex, "telegram": self.telegram, "byedpi": self.byedpi}


async def postgres_available(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def proxy_available(settings: Settings) -> bool:
    # A TCP connection is enough: sidecar must not publish its port outside Docker.
    try:
        from app.network.proxy import proxy_url
        value = proxy_url(settings, settings.telegram_bot_transport)
        if not value:
            return True
        host_port = value.rsplit(":", 1)[-2:]
        host = host_port[0].split("://", 1)[-1]
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(host_port[1])), timeout=2)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


async def yandex_available(settings: Settings) -> bool | None:
    if not (settings.configured(settings.yandex_api_key) and settings.yandex_folder_id):
        return False
    # Do not report availability merely because credentials exist. A live model
    # probe is deliberately performed by CLI test-yandex and completed in phase 5.
    return None


async def notifier_configured(settings: Settings) -> bool | None:
    if not settings.configured(settings.telegram_notify_bot_token):
        return None
    return bool(settings.telegram_notify_chat_id)
