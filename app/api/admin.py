"""Local-only operational panel and JSON endpoints (the compose file binds it to 127.0.0.1)."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from html import escape
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from app.storage.models import FeedbackEvent, Lead, LeadAnalysis, Offer, RawMessage, SourceTarget

router = APIRouter(tags=["admin"])

@router.get("/api/dashboard")
async def dashboard_data(request: Request) -> dict:
    since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with request.app.state.sessions() as s:
        async def count(model, *conditions): return int((await s.scalar(select(func.count()).select_from(model).where(*conditions))) or 0)
        return {"today": {"raw_messages": await count(RawMessage, RawMessage.created_at >= since), "candidates": await count(Lead, Lead.prefilter_score >= 5, Lead.created_at >= since), "analyses": await count(LeadAnalysis, LeadAnalysis.created_at >= since), "alerts": await count(Lead, Lead.notified_at >= since), "replies": await count(FeedbackEvent, FeedbackEvent.event == "replied", FeedbackEvent.created_at >= since), "conversations": await count(FeedbackEvent, FeedbackEvent.event == "conversation", FeedbackEvent.created_at >= since)}, "services": {"telegram": bool(request.app.state.telegram_scanner.client and request.app.state.telegram_scanner.client.is_connected()), "discourse": bool(request.app.state.discourse_adapter._task and not request.app.state.discourse_adapter._task.done()), "airtable": bool(request.app.state.airtable_adapter._task and not request.app.state.airtable_adapter._task.done()), "discord": bool(request.app.state.discord_listener_process and request.app.state.discord_listener_process.returncode is None), "reddit": bool(request.app.state.reddit_adapter.task and not request.app.state.reddit_adapter.task.done())}}

@router.get("/api/sources")
async def sources(request: Request) -> list[dict]:
    async with request.app.state.sessions() as s:
        rows = (await s.scalars(select(SourceTarget).order_by(SourceTarget.source, SourceTarget.name))).all()
    return [{"source": x.source, "name": x.name, "target_id": x.target_id, "last_success_at": x.last_success_at, "last_error": x.last_error} for x in rows]

@router.get("/api/leads")
async def leads(request: Request, source: str | None = None, min_score: int | None = None, status: str | None = None, limit: int = 50) -> list[dict]:
    q = select(Lead, RawMessage, LeadAnalysis, Offer).join(RawMessage, Lead.raw_message_id == RawMessage.id).outerjoin(LeadAnalysis, LeadAnalysis.lead_id == Lead.id).outerjoin(Offer, Offer.lead_id == Lead.id).order_by(Lead.created_at.desc()).limit(min(max(limit, 1), 200))
    if source: q = q.where(RawMessage.source == source)
    if min_score is not None: q = q.where(Lead.final_score >= min_score)
    if status: q = q.where(Lead.status == status)
    async with request.app.state.sessions() as s: rows = (await s.execute(q)).all()
    return [{"id": lead.id, "source": raw.source, "url": raw.url, "title": raw.title, "text": raw.raw_text, "score": lead.final_score, "type": lead.lead_type, "status": lead.status, "analysis": json.loads(analysis.payload) if analysis else None, "offer": offer.body if offer else None} for lead, raw, analysis, offer in rows]

@router.post("/api/leads/{lead_id}/feedback/{event}")
async def feedback(request: Request, lead_id: int, event: str) -> dict:
    if event not in {"ignored", "saved", "replied", "conversation", "call", "won", "lost"}: raise HTTPException(400, "unsupported event")
    async with request.app.state.sessions() as s:
        lead = await s.get(Lead, lead_id)
        if not lead: raise HTTPException(404, "lead not found")
        lead.status = event; s.add(FeedbackEvent(lead_id=lead.id, event=event)); await s.commit()
    return {"ok": True}

@router.get("/", response_class=HTMLResponse)
async def panel(request: Request) -> str:
    data = await dashboard_data(request); today=data["today"]; services=data["services"]
    rows = "".join(f"<tr><td>{escape(k)}</td><td>{'✅' if v else '⚠️'}</td></tr>" for k,v in services.items())
    metrics = "".join(f"<li>{escape(k.replace('_',' '))}: <b>{v}</b></li>" for k,v in today.items())
    return f"<!doctype html><meta charset=utf-8><title>Lead Radar</title><style>body{{font:16px system-ui;max-width:900px;margin:40px auto;color:#18212f}}table{{border-collapse:collapse}}td{{padding:7px 22px 7px 0}}code{{background:#f3f4f6;padding:2px 5px}}</style><h1>Lead Radar · ONLINE</h1><h2>Services</h2><table>{rows}</table><h2>Today</h2><ul>{metrics}</ul><p>JSON: <code>/api/leads</code>, <code>/api/sources</code>, <code>/api/dashboard</code>. Panel is intended only through localhost or an SSH tunnel.</p>"
