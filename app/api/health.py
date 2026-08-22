from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.network.health import HealthState, notifier_configured, postgres_available, proxy_available, yandex_available
from app.sources.telegram import load_telegram_targets
from app.sources.discourse import load_discourse_targets
from app.sources.airtable import load_airtable_targets
from app.sources.local_stream_gateway import load_discord_listener_sources

router = APIRouter(tags=["health"])


async def current_health(request: Request) -> HealthState:
    settings = request.app.state.settings
    postgres, byedpi, yandex, telegram = await asyncio.gather(
        postgres_available(request.app.state.engine), proxy_available(settings), yandex_available(settings), notifier_configured(settings)
    )
    return HealthState(postgres=postgres, byedpi=byedpi, yandex=yandex, telegram=telegram)


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    state = await current_health(request)
    return JSONResponse(state.as_dict(), status_code=status.HTTP_200_OK if state.ok else status.HTTP_503_SERVICE_UNAVAILABLE)


@router.get("/health/sources")
async def source_health(request: Request) -> dict:
    settings = request.app.state.settings
    scanner = request.app.state.telegram_scanner
    discourse = request.app.state.discourse_adapter
    airtable = request.app.state.airtable_adapter
    discord_process = request.app.state.discord_listener_process
    reddit = request.app.state.reddit_adapter
    local_gateway = request.app.state.local_gateway
    targets = load_telegram_targets(settings.sources_path)
    return {"status": "ok", "telegram": {"configured": scanner.configured,
            "connected": bool(scanner.client and scanner.client.is_connected()), "targets": len(targets),
            "resolved_targets": len(scanner.targets)},
            "discourse": {"targets": len(load_discourse_targets(settings.sources_path)),
                          "poller_running": bool(discourse._task and not discourse._task.done())},
            "airtable": {"targets": len(load_airtable_targets(settings.sources_path)),
                          "poller_running": bool(airtable._task and not airtable._task.done())},
            "discord": {"configured": settings.configured(settings.discord_user_token),
                        "targets": len(load_discord_listener_sources(settings.sources_path)),
                        "running": bool(discord_process and discord_process.returncode is None)},
            "reddit": {"targets": len(reddit.targets), "poller_running": bool(reddit.task and not reddit.task.done())},
            "local_gateway": {"configured": request.app.state.settings.configured(request.app.state.settings.auth_secret_key),
                              "running": bool(local_gateway and local_gateway.running)}}


@router.get("/health/network")
async def network_health(request: Request) -> dict:
    settings = request.app.state.settings
    return {"status": "ok" if await proxy_available(settings) else "degraded",
            "byedpi": await proxy_available(settings), "transports": {
                "telegram": settings.telegram_transport, "telegram_bot": settings.telegram_bot_transport,
                "yandex": settings.yandex_transport, "forum": settings.forum_transport}}
