from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.sources.base import RawMessageInput
from app.sources.local_stream_gateway import RawMessage
from app.storage.repository import RadarRepository


def local_stream_pipeline(sessions: async_sessionmaker):
    """Build the phase-4 handoff; scoring is attached by phase 5."""
    async def process(message: RawMessage) -> None:
        incoming = RawMessageInput(
            source="discord",
            source_name=message.source_platform,
            source_target_id=message.source_id,
            external_id=message.external_message_id,
            author_id=message.author_id,
            author_name=message.author_handle,
            author_username=message.author_handle,
            published_at=datetime.now(timezone.utc),
            text=message.payload_text,
            metadata={**message.meta_data, "source_name": message.source_platform, "ingest": "local_stream_gateway"},
            url=message.url
        )
        async with sessions() as session:
            repository = RadarRepository(session)
            await repository.ensure_target(source="discord", target_id=message.source_id,
                                           name=message.source_platform)
            await repository.save_raw_message(incoming)
            await session.commit()
    return process
