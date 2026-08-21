"""Independent, read-only Discord producer for the local stream gateway."""
from __future__ import annotations

import json
import logging

import websockets
from discord.ext import commands

from app.config import get_settings
from app.sources.local_stream_gateway import load_discord_listener_sources

logger = logging.getLogger("radar.sources.discord_listener")
settings = get_settings()
TARGET_CHANNELS = load_discord_listener_sources(settings.sources_path)
bot = commands.Bot(command_prefix="?", self_bot=True)


async def send_to_local_gateway(payload: dict) -> None:
    """Deliver one normalized packet to the same-container gateway."""
    url = f"ws://{settings.local_gateway_client_host}:{settings.local_gateway_port}"
    try:
        async with websockets.connect(url) as websocket:
            await websocket.send(settings.auth_secret_key.get_secret_value())
            await websocket.send(json.dumps(payload, ensure_ascii=False))
    except Exception as exc:
        logger.error("Could not deliver Discord packet to LocalStreamGateway: %s", exc)


@bot.event
async def on_ready():
    logger.info("Discord listener connected as %s; monitoring %d channel(s)", bot.user, len(TARGET_CHANNELS))


@bot.event
async def on_message(message):
    channel_id = str(message.channel.id)
    if channel_id not in TARGET_CHANNELS or not bot.user or message.author.id == bot.user.id or not message.content:
        return
    guild = getattr(message, "guild", None)
    payload = {
        "source_id": channel_id,
        "author_id": str(message.author.id),
        "author_handle": getattr(message.author, "name", None),
        "payload_text": message.content,
        "external_message_id": str(message.id),
        "meta_data": {
            "guild_id": str(getattr(guild, "id", "0")),
            "guild_name": str(getattr(guild, "name", "")),
            "channel_name": str(getattr(message.channel, "name", "")),
        },
    }
    logger.info("Discord message accepted from %s; forwarding to gateway", TARGET_CHANNELS[channel_id])
    await send_to_local_gateway(payload)


def start_discord_agent() -> None:
    token = settings.discord_user_token.get_secret_value()
    if not token:
        logger.warning("DISCORD_USER_TOKEN is not configured; Discord listener is disabled")
        return
    if not settings.auth_secret_key.get_secret_value():
        logger.warning("AUTH_SECRET_KEY is not configured; Discord listener is disabled")
        return
    if not TARGET_CHANNELS:
        logger.warning("No enabled discord_listener sources configured; Discord listener is disabled")
        return
    bot.run(token)


if __name__ == "__main__":
    logging.basicConfig(level=settings.log_level)
    start_discord_agent()
