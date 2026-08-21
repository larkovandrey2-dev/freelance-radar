from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.pipeline.normalize import content_hash, normalize_text
from app.sources.base import RawMessageInput
from app.storage.models import Lead, LeadAnalysis, RawMessage, SourceTarget


class RadarRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def ensure_target(self, *, source: str, target_id: str, name: str) -> SourceTarget:
        statement = insert(SourceTarget).values(source=source, target_id=target_id, name=name).on_conflict_do_nothing(
            constraint="uq_source_target"
        )
        await self.session.execute(statement)
        target = await self.session.scalar(select(SourceTarget).where(
            SourceTarget.source == source, SourceTarget.target_id == target_id
        ))
        assert target is not None
        return target

    async def save_raw_message(self, message: RawMessageInput) -> tuple[RawMessage | None, bool]:
        """Returns (message, created); duplicate updates never create a second row."""
        values = dict(source=message.source, source_target_id=message.source_target_id,
                      external_id=message.external_id, author_id=message.author_id, author_name=message.author_name,
                      author_username=message.author_username,
                      published_at=message.published_at, raw_text=message.text,
                      normalized_text=normalize_text(message.text), url=message.url,
                      title=message.title, reply_count=message.reply_count, view_count=message.view_count,
                      metadata_=message.metadata,
                      content_hash=content_hash(message.text))
        statement = insert(RawMessage).values(**values).on_conflict_do_nothing(
            constraint="uq_raw_source_target_external"
        ).returning(RawMessage.id)
        created_id = await self.session.scalar(statement)
        if created_id is None:
            return None, False
        raw_message = await self.session.get(RawMessage, created_id)
        # Every ingested message gets a durable lead row.  The pipeline decides
        # whether it is rejected, audited, pending, or notified.
        self.session.add(Lead(raw_message_id=created_id, status="queued"))
        return raw_message, True

    async def cached_analysis(self, content_hash: str) -> LeadAnalysis | None:
        return await self.session.scalar(
            select(LeadAnalysis).join(Lead, LeadAnalysis.lead_id == Lead.id).join(
                RawMessage, Lead.raw_message_id == RawMessage.id
            ).where(RawMessage.content_hash == content_hash).order_by(LeadAnalysis.id.desc()).limit(1)
        )

    async def last_seen_id(self, *, source: str, target_id: str) -> int | None:
        target = await self.session.scalar(select(SourceTarget).where(
            SourceTarget.source == source, SourceTarget.target_id == target_id
        ))
        return int(target.last_seen_message_id) if target and target.last_seen_message_id else None

    async def mark_seen(self, *, source: str, target_id: str, name: str, message_id: int) -> None:
        target = await self.ensure_target(source=source, target_id=target_id, name=name)
        if target.last_seen_message_id is None or int(target.last_seen_message_id) < message_id:
            target.last_seen_message_id = str(message_id)

    async def message_exists(self, *, source: str, target_id: str, external_id: str) -> bool:
        row = await self.session.scalar(select(RawMessage.id).where(
            RawMessage.source == source, RawMessage.source_target_id == target_id,
            RawMessage.external_id == external_id,
        ).limit(1))
        return row is not None

    async def target_http_state(self, *, source: str, target_id: str, name: str) -> SourceTarget:
        return await self.ensure_target(source=source, target_id=target_id, name=name)

    async def mark_target_success(self, target: SourceTarget, *, etag: str | None, last_modified: str | None) -> None:
        target.etag = etag or target.etag
        target.last_modified = last_modified or target.last_modified
        target.last_success_at = datetime.now(timezone.utc)
        target.last_error = None

    async def mark_target_error(self, target: SourceTarget, message: str) -> None:
        target.last_error = message[:2000]
