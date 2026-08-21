from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml
from sqlalchemy.ext.asyncio import async_sessionmaker
from telethon import TelegramClient, events, utils
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat, User

from app.config import Settings
from app.network.proxy import proxy_url
from app.sources.base import RawMessageInput
from app.storage.repository import RadarRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramTarget:
    name: str
    entity: str | int
    enabled: bool = True
    tags: tuple[str, ...] = ()


def load_telegram_targets(path: Path) -> list[TelegramTarget]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    targets = []
    for item in data.get("telegram", []):
        if "name" not in item or "entity" not in item:
            raise ValueError("Every Telegram source requires name and entity")
        targets.append(TelegramTarget(name=item["name"], entity=item["entity"], enabled=item.get("enabled", True),
                                      tags=tuple(str(tag) for tag in item.get("tags", []))))
    return [target for target in targets if target.enabled]


class TelegramScanner:
    def __init__(self, settings: Settings, sessions: async_sessionmaker):
        self.settings = settings
        self.sessions = sessions
        self.client: TelegramClient | None = None
        self.targets: dict[int, TelegramTarget] = {}

    @property
    def configured(self) -> bool:
        return bool(self.settings.telegram_api_id and self.settings.configured(self.settings.telegram_api_hash))

    def _client(self) -> TelegramClient:
        if not self.configured:
            raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")
        self.settings.telegram_session_path.parent.mkdir(parents=True, exist_ok=True)
        # Telethon accepts a per-client proxy; no app-wide proxy environment is used.
        configured_proxy = proxy_url(self.settings, self.settings.telegram_transport)
        parsed = urlparse(configured_proxy) if configured_proxy else None
        telethon_proxy = (parsed.scheme, parsed.hostname, parsed.port) if parsed else None
        return TelegramClient(str(self.settings.telegram_session_path), self.settings.telegram_api_id,
                              self.settings.telegram_api_hash.get_secret_value(),
                              proxy=telethon_proxy)

    async def start(self) -> None:
        if not self.configured:
            logger.warning("Telegram scanner disabled: credentials are not configured")
            return
        self.client = self._client()
        await self.client.connect()
        if not await self.client.is_user_authorized():
            logger.warning("Telegram scanner disabled: run `python -m app.cli telegram-login`")
            await self.client.disconnect()
            return
        await self._resolve_targets()
        self.client.add_event_handler(self._on_message, events.NewMessage(chats=list(self.targets)))
        await self._backfill()
        logger.info("Telegram scanner connected; monitoring %d target(s)", len(self.targets))

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()

    async def _resolve_targets(self) -> None:
        assert self.client
        for target in load_telegram_targets(self.settings.sources_path):
            try:
                entity = await self.client.get_entity(target.entity)
                # event.chat_id uses Telegram's signed peer ID (-100… for
                # channels); entity.id alone would silently miss all channel events.
                self.targets[utils.get_peer_id(entity)] = target
            except FloodWaitError as exc:
                logger.warning("FloodWait while resolving %s; sleeping %ss", target.name, exc.seconds)
                await asyncio.sleep(exc.seconds)
            except Exception:
                logger.exception("Unable to resolve Telegram source %s", target.name)

    async def _backfill(self) -> None:
        assert self.client
        for target_id, target in self.targets.items():
            async with self.sessions() as session:
                repo = RadarRepository(session)
                last_seen = await repo.last_seen_id(source="telegram", target_id=str(target_id))
            try:
                entity = await self.client.get_entity(target.entity)
                if last_seen:
                    # Cap a restart catch-up too: a bad cursor or a long outage
                    # must not turn into an unbounded historical import.
                    messages = self.client.iter_messages(entity, min_id=last_seen, reverse=True,
                                                         limit=self.settings.backfill_limit)
                else:
                    # For a brand-new source fetch only its newest N messages.
                    # A reverse iterator may walk a complete history.
                    messages = self.client.iter_messages(entity, limit=self.settings.backfill_limit)
                async for message in messages:
                    await self._process(message, target, entity)
            except FloodWaitError as exc:
                logger.warning("FloodWait on backfill %s; sleeping %ss", target.name, exc.seconds)
                await asyncio.sleep(exc.seconds)
            except Exception:
                logger.exception("Telegram backfill failed for %s", target.name)

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        target = self.targets.get(event.chat_id)
        if target:
            await self._process(event.message, target, await event.get_chat())

    async def _process(self, message, target: TelegramTarget, chat) -> None:
        if not message.message:
            return
        sender = await message.get_sender()
        username = getattr(sender, "username", None)
        author_name = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)])) or None
        chat_username = getattr(chat, "username", None)
        url = f"https://t.me/{chat_username}/{message.id}" if chat_username else None
        target_id = str(utils.get_peer_id(chat))
        incoming = RawMessageInput(source="telegram", source_name=target.name,
            source_target_id=target_id, external_id=str(message.id), author_id=str(getattr(sender, "id", "")) or None,
            author_name=author_name, author_username=username, published_at=message.date, url=url,
            text=message.message, view_count=getattr(message, "views", None),
            metadata={"telegram_user_url": f"https://t.me/{username}" if username else None, "tags": list(target.tags),
                      "source_name": target.name})
        async with self.sessions() as session:
            repo = RadarRepository(session)
            await repo.ensure_target(source="telegram", target_id=target_id, name=target.name)
            _, created = await repo.save_raw_message(incoming)
            await repo.mark_seen(source="telegram", target_id=target_id, name=target.name, message_id=message.id)
            await session.commit()
        if created:
            logger.info("Saved Telegram message %s from %s", message.id, target.name)
