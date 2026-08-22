"""Read-only collector for the public Airtable Community Jobs Board."""
from __future__ import annotations

import asyncio
import html
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.network.proxy import proxy_url
from app.sources.base import RawMessageInput
from app.storage.repository import RadarRepository

logger = logging.getLogger(__name__)
SOURCE = "airtable"


@dataclass(frozen=True)
class AirtableTarget:
    name: str
    category_url: str
    enabled: bool
    tags: list[str]

    @property
    def target_id(self) -> str:
        return self.category_url.rstrip("/")


def load_airtable_targets(path: Path) -> list[AirtableTarget]:
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    targets: list[AirtableTarget] = []
    for item in (data or {}).get("airtable", []):
        if not isinstance(item, dict) or "name" not in item or "category_url" not in item:
            raise ValueError("Every Airtable source requires name and category_url")
        targets.append(AirtableTarget(str(item["name"]), str(item["category_url"]).rstrip("/"),
                                      bool(item.get("enabled", True)), list(item.get("tags", []))))
    return [target for target in targets if target.enabled]


def extract_topic_urls(page: str, category_url: str) -> list[tuple[str, str]]:
    escaped = re.escape(category_url.rstrip("/"))
    pattern = rf'href="({escaped}/[^"?#]+-(\d+))(?:\?[^"#]*)?(?:#[^"]*)?"'
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for url, topic_id in re.findall(pattern, page):
        if topic_id not in seen:
            seen.add(topic_id)
            result.append((html.unescape(url), topic_id))
    return result


def topic_details(page: str) -> tuple[str, str, datetime]:
    title_match = re.search(r"<title>(.*?)\s*\|\s*Airtable Community</title>", page, re.S | re.I)
    description_match = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', page, re.I)
    date_match = re.search(r'<time[^>]+dateTime="(\d{4}-\d{2}-\d{2})"', page, re.I)
    if not title_match or not description_match:
        raise ValueError("Airtable topic page has no public title or description")
    published_at = datetime.now(timezone.utc)
    if date_match:
        published_at = datetime.fromisoformat(date_match.group(1)).replace(tzinfo=timezone.utc)
    return (html.unescape(re.sub(r"\s+", " ", title_match.group(1))).strip(),
            html.unescape(re.sub(r"\s+", " ", description_match.group(1))).strip(), published_at)


class AirtableAdapter:
    def __init__(self, settings: Settings, sessions: async_sessionmaker):
        self.settings, self.sessions = settings, sessions
        self.targets = load_airtable_targets(settings.sources_path)
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.targets:
            self._task = asyncio.create_task(self._run(), name="airtable-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            for target in self.targets:
                if self._stop.is_set():
                    return
                await self.poll_target(target)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=random.uniform(
                    self.settings.forum_poll_min_seconds, self.settings.forum_poll_max_seconds))
            except asyncio.TimeoutError:
                pass

    async def poll_target(self, target: AirtableTarget) -> None:
        async with self.sessions() as session:
            repo = RadarRepository(session)
            state = await repo.target_http_state(source=SOURCE, target_id=target.target_id, name=target.name)
            try:
                async with httpx.AsyncClient(proxy=proxy_url(self.settings, self.settings.forum_transport),
                    timeout=httpx.Timeout(15, connect=5), follow_redirects=True,
                    headers={"User-Agent": "LeadRadar/0.1 (+read-only)"}) as client:
                    listing = await client.get(target.category_url)
                    listing.raise_for_status()
                    for topic_url, topic_id in extract_topic_urls(listing.text, target.category_url):
                        if await repo.message_exists(source=SOURCE, target_id=target.target_id, external_id=topic_id):
                            continue
                        response = await client.get(topic_url)
                        response.raise_for_status()
                        title, body, published_at = topic_details(response.text)
                        await repo.save_raw_message(RawMessageInput(source=SOURCE, source_name=target.name,
                            source_target_id=target.target_id, external_id=topic_id, published_at=published_at,
                            url=topic_url, title=title, text=f"{title}\n{body}", metadata={"tags": target.tags}))
                await repo.mark_target_success(state, etag=None, last_modified=None)
                await session.commit()
            except (httpx.HTTPError, ValueError) as exc:
                await repo.mark_target_error(state, str(exc))
                await session.commit()
                logger.warning("Airtable poll failed for %s: %s", target.name, exc)
