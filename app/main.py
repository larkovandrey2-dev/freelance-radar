from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.health import router as health_router
from app.api.admin import router as admin_router
from app.config import get_settings
from app.sources.telegram import TelegramScanner
from app.storage.models import Base
from app.sources.discourse import DiscourseAdapter
from app.sources.airtable import AirtableAdapter
from app.sources.local_stream_gateway import LocalStreamGateway
from app.sources.reddit import RedditAdapter
from app.sources.local_stream_gateway import load_discord_listener_sources
from app.pipeline.ingest import local_stream_pipeline
from app.pipeline.lead_worker import LeadWorker


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessions = sessions
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # create_all does not add columns to an existing persistent volume.
        for statement in (
            "ALTER TABLE source_targets ADD COLUMN IF NOT EXISTS etag VARCHAR(512)",
            "ALTER TABLE source_targets ADD COLUMN IF NOT EXISTS last_modified VARCHAR(128)",
            "ALTER TABLE source_targets ADD COLUMN IF NOT EXISTS last_success_at TIMESTAMPTZ",
            "ALTER TABLE source_targets ADD COLUMN IF NOT EXISTS last_error TEXT",
            "ALTER TABLE raw_messages ADD COLUMN IF NOT EXISTS author_name VARCHAR(256)",
            "ALTER TABLE raw_messages ADD COLUMN IF NOT EXISTS author_username VARCHAR(256)",
            "ALTER TABLE raw_messages ADD COLUMN IF NOT EXISTS title TEXT",
            "ALTER TABLE raw_messages ADD COLUMN IF NOT EXISTS reply_count INTEGER",
            "ALTER TABLE raw_messages ADD COLUMN IF NOT EXISTS view_count INTEGER",
            "ALTER TABLE raw_messages ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ",
            "ALTER TABLE lead_analyses ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
            "ALTER TABLE offers ADD COLUMN IF NOT EXISTS language VARCHAR(16)",
            "ALTER TABLE offers ADD COLUMN IF NOT EXISTS price VARCHAR(64)",
            "ALTER TABLE offers ADD COLUMN IF NOT EXISTS deadline VARCHAR(64)",
        ):
            await connection.exec_driver_sql(statement)
    scanner = TelegramScanner(settings, sessions)
    app.state.telegram_scanner = scanner
    # Source connection failures or a slow Telegram route must never delay the
    # operational API and the other independent collectors.
    scanner_task = asyncio.create_task(scanner.start(), name="telegram-scanner")
    app.state.telegram_scanner_task = scanner_task
    discourse = DiscourseAdapter(settings, sessions)
    app.state.discourse_adapter = discourse
    await discourse.start()
    airtable = AirtableAdapter(settings, sessions)
    app.state.airtable_adapter = airtable
    await airtable.start()
    reddit = RedditAdapter(settings, sessions); app.state.reddit_adapter = reddit
    await reddit.start()
    local_gateway: LocalStreamGateway | None = None
    if settings.configured(settings.auth_secret_key):
        local_gateway = LocalStreamGateway(
            auth_secret_key=settings.auth_secret_key.get_secret_value(),
            my_internal_id=settings.my_internal_id,
            sqlite_path=settings.local_gateway_sqlite_path,
            process_raw_message=local_stream_pipeline(sessions),
            host=settings.local_gateway_host,
            port=settings.local_gateway_port,
        )
        await local_gateway.start()
    app.state.local_gateway = local_gateway
    # The external listener owns Discord connectivity. Start it only after the
    # authenticated loopback gateway is accepting packets; missing credentials
    # are a normal configuration state, not an application failure.
    discord_listener_process = None
    listener_sources = load_discord_listener_sources(settings.sources_path)
    if not settings.configured(settings.discord_user_token):
        logging.getLogger(__name__).warning("DISCORD_USER_TOKEN is not configured; Discord listener is disabled")
    elif local_gateway is None:
        logging.getLogger(__name__).warning("Discord listener requires AUTH_SECRET_KEY; listener is disabled")
    elif not listener_sources:
        logging.getLogger(__name__).warning("No enabled discord_listener sources configured; listener is disabled")
    else:
        discord_listener_process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.sources.discord_listener"
        )
        logging.getLogger(__name__).info("Discord listener started with PID %s", discord_listener_process.pid)
    app.state.discord_listener_process = discord_listener_process
    lead_worker = LeadWorker(settings, sessions)
    app.state.lead_worker = lead_worker
    await lead_worker.start()
    try:
        yield
    finally:
        await lead_worker.stop()
        scanner_task.cancel()
        try:
            await scanner_task
        except asyncio.CancelledError:
            pass
        await scanner.stop()
        await discourse.stop()
        await airtable.stop()
        await reddit.stop()
        if discord_listener_process and discord_listener_process.returncode is None:
            discord_listener_process.terminate()
            try:
                await asyncio.wait_for(discord_listener_process.wait(), timeout=10)
            except asyncio.TimeoutError:
                discord_listener_process.kill()
                await discord_listener_process.wait()
        if local_gateway:
            await local_gateway.stop()
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Lead Radar", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(admin_router)
    return app


app = create_app()


if __name__ == "__main__":
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Telegram Bot API keeps the token in the request URL; do not emit it at
    # INFO level through httpx's standard request logger.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, log_level=settings.log_level.lower())
