from __future__ import annotations

from datetime import datetime, timezone
import json

import httpx
import asyncio
import logging
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy import select

from app.config import Settings
from app.network.proxy import proxy_url
from app.storage.models import FeedbackEvent, Lead, LeadAnalysis, Offer, RawMessage
from app.pipeline.offer_generator import OfferGenerator
from app.pipeline.pricing import calculate_quote
from app.pipeline.analyzer import AnalysisError

logger = logging.getLogger(__name__)

class TelegramNotifier:
    def __init__(self, settings: Settings): self.settings = settings
    @property
    def configured(self) -> bool:
        return bool(self.settings.configured(self.settings.telegram_notify_bot_token) and self.settings.telegram_notify_chat_id)

    async def send(self, lead: Lead, raw: RawMessage, analysis: dict | None, *, unavailable: bool = False) -> None:
        if not self.configured: return
        score = lead.final_score or lead.prefilter_score or 0
        lead_type = lead.lead_type or "HIGH-CONFIDENCE MATCH"
        age = max(0, round((datetime.now(timezone.utc) - raw.published_at).total_seconds() / 60))
        if unavailable:
            description = "⚠️ AI analysis unavailable\nHigh-confidence rule match"
        else:
            description = str((analysis or {}).get("summary_ru") or raw.raw_text[:500])
        budget = (analysis or {}).get("budget") or {}
        budget_line = ""
        if budget.get("explicit"):
            budget_line = f"\n💰 Бюджет: {budget.get('min', '?')}–{budget.get('max', '?')} {budget.get('currency', '')}"
        delivery = (analysis or {}).get("delivery_confidence")
        fit = (analysis or {}).get("fit_for_user")
        risk = (analysis or {}).get("integration_risk")
        shape = (analysis or {}).get("task_shape")
        intelligence = ""
        if analysis:
            intelligence = f"\n\nМОЙ FIT: {fit if fit is not None else '?'}/10 · DELIVERY: {delivery if delivery is not None else '?'}/10\nRISK: {risk or 'UNKNOWN'} · {shape or ''}"
            reasons = (analysis or {}).get("why_can_deliver_ru") or []
            if reasons:
                intelligence += "\nПочему реально сделать:\n" + "\n".join(f"• {item}" for item in reasons[:4])
            unknowns = (analysis or {}).get("main_unknowns") or []
            if unknowns:
                intelligence += "\nЧто уточнить:\n" + "\n".join(f"⚠️ {item}" for item in unknowns[:3])
        text = (f"🔥 {score}/100 · {lead_type}\n\n🌐 {raw.source}\n⏱ {age} мин. назад\n💬 {raw.reply_count or 0} ответов\n\n"
                f"{description}{budget_line}{intelligence}\n\n👤 @{raw.author_username}" if raw.author_username else
                f"🔥 {score}/100 · {lead_type}\n\n🌐 {raw.source}\n⏱ {age} мин. назад\n\n{description}{budget_line}{intelligence}")
        keyboard = [[{"text": "👤 Автор", "url": raw.metadata_.get("author_profile_url") or raw.metadata_.get("telegram_user_url") or raw.url or "https://t.me"},
                     {"text": "🔗 Пост", "url": raw.url or "https://t.me"}],
                    [{"text": "✍️ Отклик", "callback_data": f"offer:{lead.id}"}, {"text": "⭐ Сохранить", "callback_data": f"save:{lead.id}"},
                     {"text": "❌ Мимо", "callback_data": f"skip:{lead.id}"}],
                    [{"text": "✅ Ответил", "callback_data": f"replied:{lead.id}"}]]
        async with httpx.AsyncClient(timeout=15, proxy=proxy_url(self.settings, self.settings.telegram_bot_transport)) as client:
            response = await client.post(f"https://api.telegram.org/bot{self.settings.telegram_notify_bot_token.get_secret_value()}/sendMessage",
                json={"chat_id": self.settings.telegram_notify_chat_id, "text": text[:4096], "reply_markup": {"inline_keyboard": keyboard}})
            response.raise_for_status()

    async def poll_actions(self, sessions: async_sessionmaker, stop_event: asyncio.Event) -> None:
        """Persist inline-button feedback without exposing a webhook publicly."""
        if not self.configured:
            return
        offset: int | None = None
        token = self.settings.telegram_notify_bot_token.get_secret_value()
        async with httpx.AsyncClient(timeout=35, proxy=proxy_url(self.settings, self.settings.telegram_bot_transport)) as client:
            while not stop_event.is_set():
                try:
                    response = await client.get(f"https://api.telegram.org/bot{token}/getUpdates",
                        params={"offset": offset, "timeout": 25, "allowed_updates": '["callback_query"]'})
                    response.raise_for_status()
                    for update in response.json().get("result", []):
                        offset = int(update["update_id"]) + 1
                        callback = update.get("callback_query") or {}
                        data = str(callback.get("data", ""))
                        try:
                            parts = data.split(":")
                            action, lead_id = parts[0], parts[1]
                            reason = parts[2] if action == "reason" and len(parts) == 3 else None
                            if action not in {"offer", "save", "skip", "reason", "replied", "conversation", "call", "won", "lost"}: continue
                            async with sessions() as session:
                                lead = await session.get(Lead, int(lead_id))
                                if lead:
                                    status = {"save": "saved", "skip": "ignored", "reason": "ignored", "replied": "replied", "conversation": "conversation", "call": "call", "won": "won", "lost": "lost"}.get(action)
                                    if status: lead.status = status
                                    event = "offer_generated" if action == "offer" else (reason or "ignored" if action == "skip" else action)
                                    session.add(FeedbackEvent(lead_id=lead.id, event=event))
                                    if action == "offer":
                                        existing = await session.scalar(select(Offer).where(Offer.lead_id == lead.id).order_by(Offer.id.desc()))
                                        if existing:
                                            offer_text = existing.body
                                        else:
                                            raw = await session.get(RawMessage, lead.raw_message_id)
                                            stored = await session.scalar(select(LeadAnalysis).where(LeadAnalysis.lead_id == lead.id))
                                            if not raw or not stored:
                                                raise ValueError("lead has no analysis")
                                            analysis = json.loads(stored.payload)
                                            quote = calculate_quote(analysis, self.settings)
                                            generated = await OfferGenerator(self.settings).generate(
                                                lead={"source": raw.source, "title": raw.title, "text": raw.raw_text, "url": raw.url}, analysis=analysis, quote=quote)
                                            offer_text = generated["message"]
                                            session.add(Offer(lead_id=lead.id, body=offer_text, language=generated.get("language"), price=quote.price, deadline=quote.deadline))
                                    await session.commit()
                                else:
                                    offer_text = None
                            if action == "offer" and offer_text:
                                await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": self.settings.telegram_notify_chat_id, "text": f"✍️ Отклик для лида #{lead_id}\n\n{offer_text}"[:4096]})
                            if action == "skip":
                                buttons = [[{"text": label, "callback_data": f"reason:{lead_id}:{value}"}] for value, label in (
                                    ("too_hard", "Слишком сложно"), ("too_large", "Слишком большой"), ("bad_budget", "Слабый бюджет"),
                                    ("unknown_stack", "Незнакомый стек"), ("too_much_competition", "Высокая конкуренция"),
                                    ("not_interesting", "Неинтересно"), ("not_a_real_lead", "Не лид"), ("already_taken", "Уже взяли"))]
                                await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": self.settings.telegram_notify_chat_id, "text": f"Почему мимо лид #{lead_id}?", "reply_markup": {"inline_keyboard": buttons}})
                            await client.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                                json={"callback_query_id": callback["id"], "text": "Отклик сгенерирован" if action == "offer" else "Учтено"})
                        except (ValueError, KeyError, AnalysisError) as exc:
                            logger.warning("Telegram callback failed: %s", exc)
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning("Telegram callback polling failed: %s", exc)
                    try: await asyncio.wait_for(stop_event.wait(), timeout=5)
                    except asyncio.TimeoutError: pass
