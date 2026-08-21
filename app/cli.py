from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import select
from telethon import TelegramClient

from app.config import get_settings
from app.network.health import postgres_available, proxy_available
from app.network.proxy import proxy_url
from app.sources.telegram import load_telegram_targets
from app.sources.discourse import load_discourse_targets
from app.pipeline.analyzer import AnalysisError, YandexAnalyzer
from app.pipeline.prefilter import evaluate
from app.pipeline.notifier import TelegramNotifier
from app.storage.models import Lead, LeadAnalysis, RawMessage


def telethon_proxy(settings):
    value = proxy_url(settings, settings.telegram_transport)
    if not value:
        return None
    parsed = urlparse(value)
    return parsed.scheme, parsed.hostname, parsed.port


async def telegram_login() -> None:
    settings = get_settings()
    if not settings.telegram_api_id or not settings.configured(settings.telegram_api_hash):
        raise SystemExit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first.")
    settings.telegram_session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(settings.telegram_session_path), settings.telegram_api_id,
                            settings.telegram_api_hash.get_secret_value(),
                            proxy=telethon_proxy(settings))
    await client.start(phone=lambda: input("Phone: "), password=lambda: getpass.getpass("2FA password: "))
    me = await client.get_me()
    print(f"Logged in as @{me.username or me.id}; session saved at {settings.telegram_session_path}.session")
    await client.disconnect()


async def health() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        print({"postgres": await postgres_available(engine), "byedpi": await proxy_available(settings)})
    finally:
        await engine.dispose()


async def test_network() -> None:
    settings = get_settings()
    print({"byedpi": await proxy_available(settings), "byedpi_proxy": settings.byedpi_proxy})


async def test_yandex() -> None:
    settings = get_settings()
    if not settings.configured(settings.yandex_api_key) or not settings.active_yandex_model_uri:
        raise SystemExit("Set YANDEX_API_KEY (or YANDEX_CLOUD_API_KEY) and model URI first.")
    if not settings.yandex_uses_openai_compat and not settings.yandex_folder_id:
        raise SystemExit("Set YANDEX_FOLDER_ID for the native Completion API.")
    payload = {
        "modelUri": settings.resolved_yandex_model_uri,
        "completionOptions": {"stream": False, "temperature": 0.1, "maxTokens": 32,
                              "reasoningOptions": {"mode": "DISABLED"}},
        "jsonObject": True,
        "messages": [{"role": "user", "text": "Return exactly this JSON object: {\"ok\": true}"}],
    }
    url = "https://ai.api.cloud.yandex.net/foundationModels/v1/completion"
    if settings.yandex_uses_openai_compat:
        payload = {"model": settings.active_yandex_model_uri,
                   "messages": [{"role": "user", "content": "Return exactly this JSON object: {\"ok\": true}"}],
                   "temperature": 0.1, "max_tokens": 256, "response_format": {"type": "json_object"},
                   "reasoning_effort": "none"}
        url = settings.yandex_openai_base_url.rstrip("/") + "/chat/completions"
    proxy = proxy_url(settings, settings.yandex_transport)
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=15, proxy=proxy) as client:
        response = await client.post(url,
            headers={"Authorization": f"Api-Key {settings.yandex_api_key.get_secret_value()}"}, json=payload)
        if response.is_error:
            raise SystemExit(f"Yandex HTTP {response.status_code}: {response.text[:500]}")
        response.raise_for_status()
        body = response.json()
    try:
        if settings.yandex_uses_openai_compat:
            choice = body["choices"][0]
            text = choice["message"].get("content")
            if not isinstance(text, str):
                raise SystemExit(f"Yandex returned empty content (finish_reason={choice.get('finish_reason')!r}). "
                                 "The model likely spent its output budget on reasoning.")
        else:
            text = body["result"]["alternatives"][0]["message"]["text"]
        if text.strip().startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        assert json.loads(text).get("ok") is True
    except (KeyError, IndexError, ValueError, AssertionError, TypeError) as exc:
        raise SystemExit(f"Yandex model returned invalid JSON: {exc}") from exc
    mode = "OpenAI-compatible" if settings.yandex_uses_openai_compat else "native Completion"
    print(f"Yandex {mode} model {settings.active_yandex_model_uri} answered valid JSON in {round((time.perf_counter() - started) * 1000)}ms.")


async def test_notifier() -> None:
    settings = get_settings()
    token = settings.telegram_notify_bot_token.get_secret_value()
    if not token or not settings.telegram_notify_chat_id:
        raise SystemExit("Set TELEGRAM_NOTIFY_BOT_TOKEN and TELEGRAM_NOTIFY_CHAT_ID first.")
    proxy = proxy_url(settings, settings.telegram_bot_transport)
    async with httpx.AsyncClient(timeout=15, proxy=proxy) as client:
        response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
            "chat_id": settings.telegram_notify_chat_id, "text": "✅ Lead Radar notifier test",
        })
        response.raise_for_status()
    print("Test notification sent.")


async def test_lead_alert() -> None:
    """Send one button-enabled alert for an already analyzed forum post.

    This deliberately does not alter the lead status or notified_at: it is a
    transport/rendering smoke test, not a production delivery.
    """
    settings = get_settings()
    notifier = TelegramNotifier(settings)
    if not notifier.configured:
        raise SystemExit("Set TELEGRAM_NOTIFY_BOT_TOKEN and TELEGRAM_NOTIFY_CHAT_ID first.")
    engine = create_async_engine(settings.database_url)
    try:
        from sqlalchemy.ext.asyncio import async_sessionmaker
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            row = (await session.execute(
                select(Lead, RawMessage, LeadAnalysis)
                .join(RawMessage, Lead.raw_message_id == RawMessage.id)
                .join(LeadAnalysis, LeadAnalysis.lead_id == Lead.id)
                .where(RawMessage.source == "discourse")
                .order_by(Lead.final_score.desc().nullslast(), Lead.id.desc())
                .limit(1)
            )).first()
        if row is None:
            raise SystemExit("No analyzed forum lead available for the alert test.")
        lead, raw, stored_analysis = row
        await notifier.send(lead, raw, json.loads(stored_analysis.payload))
        print(f"Button-enabled test alert sent for real forum post: lead={lead.id}, url={raw.url}")
    finally:
        await engine.dispose()


async def sources() -> None:
    settings = get_settings()
    for target in load_telegram_targets(settings.sources_path):
        print(f"telegram  {target.name:<24} {target.entity}")
    for target in load_discourse_targets(settings.sources_path):
        print(f"discourse  {target.name:<24} {target.category_url}")


async def audit_forum() -> None:
    """Audit 20 real forum posts without creating leads or sending alerts."""
    settings = get_settings()
    analyzer = YandexAnalyzer(settings)
    if not analyzer.configured:
        raise SystemExit("Configure Yandex before running audit-forum.")
    engine = create_async_engine(settings.database_url)
    try:
        from sqlalchemy.ext.asyncio import async_sessionmaker
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            rows = list((await session.scalars(select(RawMessage).where(RawMessage.source == "discourse")
                .order_by(RawMessage.published_at.desc()).limit(250))).all())
        scored = [(row, evaluate(row.raw_text, row.title)) for row in rows]
        candidates = sorted((item for item in scored if item[1].candidate), key=lambda item: item[1].score, reverse=True)[:10]
        # Highest-scoring rejected entries are the most valuable false-negative check.
        rejected = sorted((item for item in scored if not item[1].candidate), key=lambda item: item[1].score, reverse=True)[:10]
        selected = candidates + rejected
        print(f"Auditing {len(selected)} live Discourse posts: {len(candidates)} candidates + {len(rejected)} borderline rejections")
        for index, (raw, prefilter) in enumerate(selected, start=1):
            age = f"{max(0, round((datetime.now(timezone.utc) - raw.published_at).total_seconds() / 60))} minutes"
            try:
                analysis, _ = await analyzer.analyze(source=raw.source, message=raw.raw_text, title=raw.title,
                    age=age, reply_count=raw.reply_count, signals=prefilter.signals)
                print(json.dumps({"n": index, "id": raw.id, "prefilter": prefilter.score,
                    "candidate": prefilter.candidate, "relevant": analysis.get("relevant"),
                    "lead_type": analysis.get("lead_type"), "intent": analysis.get("purchase_intent"),
                    "fit": analysis.get("fit"), "urgency": analysis.get("urgency"), "title": raw.title,
                    "summary_ru": analysis.get("summary_ru")}, ensure_ascii=False))
            except AnalysisError as exc:
                print(json.dumps({"n": index, "id": raw.id, "error": str(exc), "title": raw.title}, ensure_ascii=False))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("command", choices=["health", "test-network", "test-yandex", "test-notifier", "test-lead-alert", "sources", "telegram-login", "audit-forum"])
    args = parser.parse_args()
    logging.basicConfig(level="INFO")
    # Bot API tokens are part of Telegram endpoint paths.  Keep HTTP client
    # request URLs out of normal logs so secrets cannot leak through stdout.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(globals()[args.command.replace("-", "_")]())


if __name__ == "__main__":
    main()
