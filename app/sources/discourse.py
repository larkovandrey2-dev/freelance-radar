from __future__ import annotations

import asyncio
import html
import logging
import random
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.network.proxy import proxy_url
from app.sources.base import RawMessageInput
from app.storage.repository import RadarRepository

logger = logging.getLogger(__name__)
RETRY_DELAYS = (1, 3, 10, 30)


@dataclass(frozen=True)
class DiscourseTarget:
    name: str
    category_url: str
    enabled: bool
    tags: list[str]

    @property
    def target_id(self) -> str:
        return self.category_url.rstrip("/")

    @property
    def base_url(self) -> str:
        parsed = urlparse(self.category_url)
        return f"{parsed.scheme}://{parsed.netloc}"


def load_discourse_targets(path: Path) -> list[DiscourseTarget]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    targets: list[DiscourseTarget] = []
    for item in data.get("discourse", []):
        if not isinstance(item, dict) or "name" not in item or "category_url" not in item:
            raise ValueError("Every Discourse source requires name and category_url")
        url = str(item["category_url"]).rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(f"Discourse category_url must be HTTPS: {url}")
        targets.append(DiscourseTarget(name=str(item["name"]), category_url=url,
                       enabled=bool(item.get("enabled", True)), tags=list(item.get("tags", []))))
    return [target for target in targets if target.enabled]


def plain_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except ValueError:
        return parsedate_to_datetime(value).astimezone(timezone.utc)


def public_contacts(value: str) -> list[dict[str, str]]:
    """Keep only contact details the author placed in the public topic body."""
    patterns = {
        "email": r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "telegram": r"https?://(?:t\.me|telegram\.me)/[A-Za-z0-9_]{3,}",
        "discord": r"https?://(?:discord\.gg|discord\.com/invite)/[A-Za-z0-9-]+",
        "x": r"https?://(?:x\.com|twitter\.com)/[A-Za-z0-9_]+",
        "linkedin": r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9-]+/?",
        "calendly": r"https?://calendly\.com/[A-Za-z0-9_/-]+",
    }
    contacts: list[dict[str, str]] = []
    for kind, pattern in patterns.items():
        for match in re.finditer(pattern, value, flags=re.IGNORECASE):
            contacts.append({"kind": kind, "value": match.group(0)})
    return contacts


def competition_score(created_at: datetime, reply_count: int) -> int:
    age_minutes = max(0, (datetime.now(timezone.utc) - created_at).total_seconds() / 60)
    if age_minutes < 5 and reply_count == 0:
        return 10
    if age_minutes < 20 and reply_count <= 2:
        return 9
    if reply_count >= 20:
        return 3
    return 6


class DiscourseAdapter:
    """Low-volume public Discourse collector; it never authenticates or posts."""
    def __init__(self, settings: Settings, sessions: async_sessionmaker):
        self.settings = settings
        self.sessions = sessions
        self.targets = load_discourse_targets(settings.sources_path)
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.targets:
            logger.info("No enabled Discourse sources configured")
            return
        self._task = asyncio.create_task(self._run(), name="discourse-poller")

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
            delay = random.uniform(self.settings.forum_poll_min_seconds, self.settings.forum_poll_max_seconds)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def poll_target(self, target: DiscourseTarget) -> None:
        async with self.sessions() as session:
            repo = RadarRepository(session)
            state = await repo.target_http_state(source="discourse", target_id=target.target_id, name=target.name)
            headers = {"Accept": "application/json", "User-Agent": "LeadRadar/0.1 (+read-only)"}
            if state.etag:
                headers["If-None-Match"] = state.etag
            if state.last_modified:
                headers["If-Modified-Since"] = state.last_modified
            try:
                response = await self._fetch(target.category_url + ".json", headers)
                if response.status_code == 304:
                    await repo.mark_target_success(state, etag=None, last_modified=None)
                    await session.commit()
                    return
                response.raise_for_status()
                topics = self._extract_topics(response.json())
                for topic in topics:
                    topic_id = str(topic["id"])
                    if await repo.message_exists(source="discourse", target_id=target.target_id, external_id=topic_id):
                        continue
                    details = await self._fetch_topic(target, topic_id)
                    message = self._to_raw_message(target, topic, details)
                    await repo.save_raw_message(message)
                await repo.mark_target_success(state, etag=response.headers.get("ETag"),
                                               last_modified=response.headers.get("Last-Modified"))
                await session.commit()
            except (httpx.HTTPError, ValueError, KeyError, ET.ParseError) as exc:
                logger.warning("Discourse poll failed for %s: %s", target.name, exc)
                try:
                    await self._poll_rss(target, repo, state)
                    await session.commit()
                except Exception as rss_exc:
                    await repo.mark_target_error(state, f"JSON: {exc}; RSS: {rss_exc}")
                    await session.commit()
                    logger.warning("Discourse RSS fallback failed for %s: %s", target.name, rss_exc)

    async def _fetch(self, url: str, headers: dict[str, str]) -> httpx.Response:
        proxy = proxy_url(self.settings, self.settings.forum_transport)
        async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(15, connect=5), follow_redirects=True) as client:
            for index, base_delay in enumerate(RETRY_DELAYS, start=1):
                response = await client.get(url, headers=headers)
                if response.status_code not in (429, 500, 502, 503, 504):
                    return response
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else base_delay + random.random()
                logger.info("Discourse %s returned %s; retry %d in %.1fs", url, response.status_code, index, delay)
                await asyncio.sleep(delay)
            return response

    async def _fetch_topic(self, target: DiscourseTarget, topic_id: str) -> dict:
        response = await self._fetch(urljoin(target.base_url, f"/t/{topic_id}.json"), {"Accept": "application/json"})
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _extract_topics(payload: dict) -> list[dict]:
        topics = payload.get("topic_list", {}).get("topics")
        if not isinstance(topics, list):
            raise ValueError("Unexpected Discourse category JSON schema")
        return [topic for topic in topics if "id" in topic and "title" in topic]

    def _to_raw_message(self, target: DiscourseTarget, topic: dict, details: dict) -> RawMessageInput:
        posts = details.get("post_stream", {}).get("posts", [])
        first = posts[0] if posts else {}
        username = first.get("username") or topic.get("last_poster_username")
        topic_id = str(topic["id"])
        slug = details.get("slug") or topic.get("slug") or topic_id
        topic_url = urljoin(target.base_url, f"/t/{slug}/{topic_id}")
        published_at = parse_datetime(first.get("created_at") or topic.get("created_at"))
        body = plain_text(first.get("cooked") or "")
        reply_count = int(topic.get("reply_count") or 0)
        return RawMessageInput(source="discourse", source_name=target.name, source_target_id=target.target_id,
            external_id=topic_id, author_id=str(first.get("user_id")) if first.get("user_id") else None,
            author_name=first.get("name"), author_username=username,
            published_at=published_at, url=topic_url, title=str(topic["title"]), text=body,
            reply_count=reply_count, view_count=int(topic.get("views") or 0),
            metadata={"tags": topic.get("tags", []) + target.tags,
                      "author_profile_url": urljoin(target.base_url, f"/u/{username}") if username else None,
                      "topic_url": topic_url, "public_contacts": public_contacts(body),
                      "competition_score": competition_score(published_at, reply_count)})

    async def _poll_rss(self, target: DiscourseTarget, repo: RadarRepository, state) -> None:
        response = await self._fetch(target.category_url + ".rss", {"Accept": "application/rss+xml"})
        response.raise_for_status()
        root = ET.fromstring(response.content)
        items = root.findall(".//item")
        for item in items:
            guid = item.findtext("guid") or item.findtext("link")
            title = item.findtext("title")
            link = item.findtext("link")
            if not guid or not title:
                continue
            # Discourse RSS guid values are often `host-topic-123`, while links
            # use `/slug/123`; normalize both to the same topic ID as JSON.
            topic_id = re.search(r"(?:/|topic-)(\d+)(?:$|[?#])", guid)
            external_id = topic_id.group(1) if topic_id else guid
            if await repo.message_exists(source="discourse", target_id=target.target_id, external_id=external_id):
                continue
            description = item.findtext("description") or ""
            body = plain_text(description)
            published_at = parse_datetime(item.findtext("pubDate"))
            await repo.save_raw_message(RawMessageInput(source="discourse", source_name=target.name,
                source_target_id=target.target_id, external_id=external_id, published_at=published_at,
                url=link, title=title, text=body, metadata={"tags": target.tags, "rss_fallback": True,
                "public_contacts": public_contacts(body)}))
        await repo.mark_target_success(state, etag=response.headers.get("ETag"), last_modified=response.headers.get("Last-Modified"))
