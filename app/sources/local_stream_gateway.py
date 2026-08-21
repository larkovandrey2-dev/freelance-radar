"""Authenticated local WebSocket ingress for approved internal text streams."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
import yaml
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

logger = logging.getLogger(__name__)

def load_discord_listener_sources(path: Path) -> dict[str, str]:
    """Return the allow-list: Discord channel ID -> stable source name."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    sources: dict[str, str] = {}
    for item in data.get("discord_listener", []):
        if not isinstance(item, Mapping) or not item.get("enabled", True):
            continue
        channel_id, name = str(item.get("channel_id", "")).strip(), str(item.get("name", "")).strip()
        if not channel_id or not name:
            raise ValueError("Every discord_listener source requires name and channel_id")
        sources[channel_id] = name
    return sources


@dataclass(frozen=True)
class RawMessage:
    """A normalized text lead accepted from an approved local producer."""

    source_platform: str
    payload_text: str
    external_message_id: str
    author_handle: str | None
    meta_data: dict[str, str]
    source_id: str
    author_id: str
    url: str


class PacketValidationError(ValueError):
    """The packet doesn't conform to the local stream contract."""


PipelineCallback = Callable[[RawMessage], Awaitable[None]]


class LocalStreamGateway:
    """A persistent, authenticated local WebSocket server for internal CRM streams.

    The producer must send the exact access token as its first text frame. Each
    later frame is a JSON packet. A SQLite primary key is committed before the
    message reaches the pipeline, making reconnect/replay delivery idempotent.
    """

    def __init__(
        self,
        *,
        auth_secret_key: str,
        my_internal_id: str,
        sqlite_path: str | Path,
        process_raw_message: PipelineCallback,
        host: str = "127.0.0.1",
        port: int = 8765,
        allowed_sources: Mapping[str, str] | None = None,
    ) -> None:
        if not auth_secret_key:
            raise ValueError("AUTH_SECRET_KEY must be configured before starting the local gateway")
        self._auth_secret_key = auth_secret_key
        self._my_internal_id = str(my_internal_id)
        self._sqlite_path = Path(sqlite_path)
        self._process_raw_message = process_raw_message
        self._host = host
        self._port = port
        self._allowed_sources = dict(allowed_sources or {})
        self._server: Server | None = None
        self._database: aiosqlite.Connection | None = None
        self._database_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        if self._server:
            return
        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._database = await aiosqlite.connect(self._sqlite_path)
        await self._database.execute("PRAGMA journal_mode=WAL")
        await self._database.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_messages (
                external_message_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await self._database.commit()
        self._server = await serve(self._handle_connection, self._host, self._port, max_size=256 * 1024)
        logger.info("Local stream gateway listening on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._database:
            await self._database.close()
            self._database = None

    def validate_and_normalize(self, raw_json: str | bytes) -> RawMessage | None:
        """Return a normalized packet, or None for a deliberate DROP decision."""
        try:
            packet = json.loads(raw_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PacketValidationError("packet must be valid JSON") from exc
        if not isinstance(packet, Mapping):
            raise PacketValidationError("packet must be a JSON object")

        source_id = self._required_string(packet, "source_id")
        author_id = self._required_string(packet, "author_id")
        if source_id not in self._allowed_sources or author_id == self._my_internal_id:
            return None

        meta_data = packet.get("meta_data", {})
        if not isinstance(meta_data, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in meta_data.items()):
            raise PacketValidationError("meta_data must be an object with string keys and values")

        author_handle = packet.get("author_handle")
        if author_handle is not None and (not isinstance(author_handle, str) or not author_handle.strip()):
            raise PacketValidationError("author_handle must be a non-empty string when supplied")

        guild_id = meta_data.get("guild_id", "0")
        channel_id = source_id  # source_id у нас и будет выступать как channel_id
        msg_id = packet.get("external_message_id", "0")

        generated_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{msg_id}"

        return RawMessage(
            source_platform=self._allowed_sources[source_id],
            payload_text=self._required_string(packet, "payload_text"),
            external_message_id=self._required_string(packet, "external_message_id"),
            author_handle=author_handle.strip() if author_handle else None,
            meta_data=dict(meta_data),
            source_id=source_id,
            author_id=author_id,
            url=generated_url,
        )

    @staticmethod
    def _required_string(packet: Mapping[str, Any], field: str) -> str:
        value = packet.get(field)
        if not isinstance(value, str) or not value.strip():
            raise PacketValidationError(f"{field} must be a non-empty string")
        return value.strip()

    async def _handle_connection(self, websocket: ServerConnection) -> None:
        try:
            token = await asyncio.wait_for(websocket.recv(), timeout=10)
            if not isinstance(token, str) or not secrets.compare_digest(token, self._auth_secret_key):
                await websocket.close(code=4401, reason="unauthorized")
                return

            logger.info("Authenticated local stream connected")
            async for frame in websocket:
                try:
                    message = self.validate_and_normalize(frame)
                    if message is None:
                        continue
                    if await self._mark_seen(message):
                        try:
                            await self._process_raw_message(message)
                        except Exception:
                            # The producer may replay after a transient pipeline
                            # failure; don't turn that retry into a permanent drop.
                            await self._unmark_seen(message.external_message_id)
                            raise
                except PacketValidationError as exc:
                    logger.warning("Dropped invalid local stream packet: %s", exc)
                except Exception:
                    # A bad downstream handler must not close a long-lived CRM stream.
                    logger.exception("Failed to process local stream packet")
        except ConnectionClosedOK:
            return
        except ConnectionClosed:
            logger.info("Local stream connection closed")
        except asyncio.TimeoutError:
            await websocket.close(code=4401, reason="authentication timeout")
        except Exception:
            logger.exception("Local stream connection failed")

    async def _mark_seen(self, message: RawMessage) -> bool:
        if self._database is None:
            raise RuntimeError("Local gateway database is not initialized")
        async with self._database_lock:
            cursor = await self._database.execute(
                "INSERT OR IGNORE INTO seen_messages (external_message_id, source_id) VALUES (?, ?)",
                (message.external_message_id, message.source_id),
            )
            await self._database.commit()
            return cursor.rowcount == 1

    async def _unmark_seen(self, external_message_id: str) -> None:
        if self._database is None:
            return
        async with self._database_lock:
            await self._database.execute(
                "DELETE FROM seen_messages WHERE external_message_id = ?", (external_message_id,)
            )
            await self._database.commit()
