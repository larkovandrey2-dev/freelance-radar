"""Passive Discord user-client collector. It never sends messages or reactions."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.sources.base import RawMessageInput
from app.storage.repository import RadarRepository

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class DiscordTarget:
    name: str; guild_id: str; channel_id: str; enabled: bool = True

def load_discord_targets(path: Path) -> list[DiscordTarget]:
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    return [DiscordTarget(str(x["name"]), str(x["guild_id"]), str(x["channel_id"]), bool(x.get("enabled", True)))
            for x in (data or {}).get("discord", []) if x.get("enabled", True)]

class DiscordUserAdapter:
    def __init__(self, settings: Settings, sessions: async_sessionmaker):
        self.settings, self.sessions = settings, sessions
        self.targets = {x.channel_id: x for x in load_discord_targets(settings.sources_path)}
        self.client = None

    @property
    def configured(self) -> bool: return bool(self.settings.configured(self.settings.discord_user_token) and self.targets)

    async def start(self) -> None:
        if not self.configured: return
        try:
            import discord  # supplied by discord.py-self, deliberately optional until configured
        except ImportError:
            logger.error("Discord configured but discord.py-self is not installed")
            return
        adapter = self
        class Client(discord.Client):
            async def on_message(self, message):
                target = adapter.targets.get(str(message.channel.id))
                if not target or self.user and message.author.id == self.user.id or not message.content: return
                incoming = RawMessageInput(source="discord", source_name=getattr(message.guild, "name", target.name), source_target_id=str(message.channel.id), external_id=str(message.id), author_id=str(message.author.id), author_name=getattr(message.author, "display_name", None), author_username=getattr(message.author, "name", None), published_at=message.created_at, text=message.content, url=f"https://discord.com/channels/{target.guild_id}/{target.channel_id}/{message.id}", metadata={"guild_id": target.guild_id, "channel_id": target.channel_id})
                async with adapter.sessions() as session:
                    repo = RadarRepository(session); await repo.ensure_target(source="discord", target_id=target.channel_id, name=target.name)
                    await repo.save_raw_message(incoming); await session.commit()
        self.client = Client()
        # start() is intentionally a background gateway listener, not REST history polling.
        import asyncio
        asyncio.create_task(self.client.start(self.settings.discord_user_token.get_secret_value(), bot=False), name="discord-user-listener")

    async def stop(self) -> None:
        if self.client: await self.client.close()
