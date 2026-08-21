from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.pipeline.analyzer import AnalysisError, YandexAnalyzer
from app.pipeline.notifier import TelegramNotifier
from app.pipeline.prefilter import evaluate
from app.storage.models import LLMUsage, Lead, LeadAnalysis, RawMessage
from app.storage.repository import RadarRepository

logger = logging.getLogger(__name__)
RETRY_DELAYS = (5, 20, 60, 300)

class LeadWorker:
    """Persistent lead state machine; safe to restart without duplicate alerts."""
    def __init__(self, settings: Settings, sessions: async_sessionmaker):
        self.settings, self.sessions = settings, sessions
        self.analyzer, self.notifier = YandexAnalyzer(settings), TelegramNotifier(settings)
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.callback_task: asyncio.Task | None = None
        self._audit_date = None
        self._audited = 0

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="lead-worker")
        self.callback_task = asyncio.create_task(self.notifier.poll_actions(self.sessions, self.stop_event), name="telegram-feedback")

    async def stop(self) -> None:
        self.stop_event.set()
        if self.task:
            self.task.cancel()
            try: await self.task
            except asyncio.CancelledError: pass
        if self.callback_task:
            self.callback_task.cancel()
            try: await self.callback_task
            except asyncio.CancelledError: pass

    async def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                processed = await self.process_one()
            except Exception:
                logger.exception("Lead worker iteration failed")
                processed = False
            if not processed:
                try: await asyncio.wait_for(self.stop_event.wait(), timeout=2)
                except asyncio.TimeoutError: pass

    def _audit_allowed(self) -> bool:
        today = datetime.now(timezone.utc).date()
        if self._audit_date != today: self._audit_date, self._audited = today, 0
        if self._audited >= self.settings.rejected_audit_daily_limit: return False
        if random.random() <= self.settings.rejected_audit_rate:
            self._audited += 1
            return True
        return False

    async def process_one(self) -> bool:
        now = datetime.now(timezone.utc)
        async with self.sessions() as session:
            row = await session.execute(select(Lead, RawMessage).join(RawMessage, Lead.raw_message_id == RawMessage.id)
                .where(Lead.status.in_(("queued", "pending")), (Lead.next_attempt_at.is_(None)) | (Lead.next_attempt_at <= now))
                .order_by(Lead.created_at).limit(1))
            item = row.first()
            if not item: return False
            lead, raw = item
            prefilter = evaluate(raw.raw_text, raw.title, raw.metadata_.get("tags", []))
            lead.prefilter_score = prefilter.score
            # Audit sampling is for learning missed buyer intent, never for
            # surfacing explicit resumes / service offers to the user.
            if prefilter.signals["negative"]:
                lead.status = "rejected"
                await session.commit()
                return True
            if not prefilter.candidate and not self._audit_allowed():
                lead.status = "rejected"
                await session.commit()
                return True
            cached = await RadarRepository(session).cached_analysis(raw.content_hash)
            try:
                if cached:
                    analysis = json.loads(cached.payload)
                else:
                    age = f"{max(0, round((now - raw.published_at).total_seconds() / 60))} minutes"
                    analysis, usage = await self.analyzer.analyze(source=raw.source, message=raw.raw_text, title=raw.title,
                        age=age, reply_count=raw.reply_count, signals=prefilter.signals)
                    session.add(LLMUsage(model=self.settings.active_yandex_model_uri, **usage))
                analysis = self._enrich_analysis(analysis)
                lead.lead_type = str(analysis.get("lead_type", "NOISE"))
                lead.final_score = self._score(analysis, raw)
                session.add(LeadAnalysis(lead_id=lead.id, model=self.settings.active_yandex_model_uri,
                    payload=json.dumps(analysis, ensure_ascii=False)))
                age_hours = max(0, (now - raw.published_at).total_seconds() / 3600)
                if self._should_alert(analysis, raw, lead.final_score, age_hours):
                    await self.notifier.send(lead, raw, analysis)
                    lead.notified_at, lead.status = now, "notified"
                else:
                    lead.status = "analyzed"
                await session.commit()
            except AnalysisError as exc:
                await self._handle_analysis_failure(session, lead, raw, prefilter.score, str(exc))
            return True

    @staticmethod
    def _enrich_analysis(analysis: dict) -> dict:
        """Keep old cached analyses compatible with the personal-fit schema."""
        result = dict(analysis)
        result.setdefault("fit_for_user", result.get("fit", 0))
        result.setdefault("delivery_confidence", result.get("fit_for_user", result.get("fit", 0)))
        result.setdefault("delegation_probability", result.get("purchase_intent", 0))
        result.setdefault("task_shape", {"small": "SMALL_PROJECT", "medium": "MEDIUM_PROJECT", "large": "LARGE_PROJECT"}.get(result.get("complexity"), "UNKNOWN"))
        result.setdefault("integration_risk", "UNKNOWN")
        result.setdefault("learning_cost", "UNKNOWN")
        for key, default in (("known_components", []), ("unknown_integrations", []), ("main_unknowns", []),
                             ("why_can_deliver_ru", []), ("risk_explanation_ru", ""),
                             ("unknowns_are_learnable", False), ("requires_client_credentials", False),
                             ("requires_paid_accounts_for_testing", False), ("requires_enterprise_expertise", False)):
            result.setdefault(key, default)
        return result

    def _should_alert(self, analysis: dict, raw: RawMessage, score: int, age_hours: float) -> bool:
        if not bool(analysis.get("relevant")) or age_hours > self.settings.max_alert_age_hours:
            return False
        delivery = float(analysis.get("delivery_confidence", 0))
        high_value_risk = score >= 85 and delivery >= 5
        if score < self.settings.lead_alert_threshold or (delivery < 7 and not high_value_risk):
            return False
        tags = {str(tag).lower() for tag in raw.metadata_.get("tags", [])}
        if "shadow_leads" in tags and float(analysis.get("delegation_probability", 0)) < 7:
            return False
        if analysis.get("task_shape") == "TOO_LARGE" and not high_value_risk:
            return False
        return True

    def _score(self, analysis: dict, raw: RawMessage) -> int:
        freshness = max(0, 10 - int((datetime.now(timezone.utc) - raw.published_at).total_seconds() // 3600))
        competition = int(raw.metadata_.get("competition_score", 6) or 6)
        shape_score = {"MICRO_TASK": 10, "SMALL_PROJECT": 9, "MEDIUM_PROJECT": 6,
                       "LARGE_PROJECT": 2, "TOO_LARGE": 0}.get(analysis.get("task_shape"), 4)
        bonus = {"MICRO_TASK": 6, "SMALL_PROJECT": 5, "VIBECODE_RESCUE": 5, "AGENCY_OVERFLOW": 6}.get(analysis.get("task_shape"), 0)
        bonus += {"VIBECODE_RESCUE": 5, "AGENCY_OVERFLOW": 6}.get(analysis.get("lead_type"), 0)
        penalty = {"HIGH": 10}.get(analysis.get("integration_risk"), 0)
        if analysis.get("task_shape") == "TOO_LARGE": penalty += 15
        if analysis.get("requires_enterprise_expertise"): penalty += 15
        score = (float(analysis.get("purchase_intent", 0)) * 2 + float(analysis.get("delivery_confidence", 0)) * 2.5 +
                 float(analysis.get("fit_for_user", 0)) * 2 + shape_score * 1.2 + freshness + competition * .7 +
                 float(analysis.get("urgency", 0)) * .6 + bonus - penalty)
        return max(0, min(100, round(score)))

    async def _handle_analysis_failure(self, session, lead: Lead, raw: RawMessage, prefilter_score: int, error: str) -> None:
        logger.warning("Analysis unavailable for raw message %s: %s", raw.id, error)
        if prefilter_score >= 12 and lead.notified_at is None:
            lead.final_score, lead.lead_type = min(100, prefilter_score * 7), "HIGH_CONFIDENCE_RULE_MATCH"
            await self.notifier.send(lead, raw, None, unavailable=True)
            lead.notified_at, lead.status = datetime.now(timezone.utc), "notified"
        else:
            delay = RETRY_DELAYS[min(lead.retry_count, len(RETRY_DELAYS) - 1)]
            lead.retry_count += 1
            lead.next_attempt_at = datetime.fromtimestamp(datetime.now().timestamp() + delay, tz=timezone.utc)
            lead.status = "pending"
        await session.commit()
