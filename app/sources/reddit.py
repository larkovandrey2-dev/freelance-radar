"""Replaceable read-only Reddit JSON adapter; no core pipeline dependency on an API SDK."""
from __future__ import annotations
import asyncio, logging, random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import httpx, yaml
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.config import Settings
from app.network.proxy import proxy_url
from app.sources.base import RawMessageInput
from app.storage.repository import RadarRepository
logger = logging.getLogger(__name__)
@dataclass(frozen=True)
class RedditTarget: name: str; subreddit: str; enabled: bool = True
def load_reddit_targets(path: Path) -> list[RedditTarget]:
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    return [RedditTarget(str(x.get("name", x["subreddit"])), str(x["subreddit"]).removeprefix("r/"), bool(x.get("enabled", True))) for x in (data or {}).get("reddit", []) if x.get("enabled", True)]
class RedditAdapter:
    def __init__(self, settings: Settings, sessions: async_sessionmaker): self.settings,self.sessions=settings,sessions; self.targets=load_reddit_targets(settings.sources_path); self.stop_event=asyncio.Event(); self.task=None
    async def start(self):
        if self.targets: self.task=asyncio.create_task(self.run(), name="reddit-poller")
    async def stop(self):
        self.stop_event.set()
        if self.task: self.task.cancel()
    async def run(self):
        while not self.stop_event.is_set():
            for target in self.targets:
                try: await self.poll(target)
                except Exception as exc: logger.warning("Reddit poll %s failed: %s", target.name, exc)
            try: await asyncio.wait_for(self.stop_event.wait(), timeout=random.uniform(self.settings.reddit_poll_min_seconds,self.settings.reddit_poll_max_seconds))
            except asyncio.TimeoutError: pass
    async def poll(self, target):
        async with httpx.AsyncClient(timeout=15, proxy=proxy_url(self.settings,self.settings.reddit_transport), headers={"User-Agent":"LeadRadar/0.1 read-only"}) as client:
            response=await client.get(f"https://www.reddit.com/r/{target.subreddit}/new.json?limit=25"); response.raise_for_status()
        async with self.sessions() as session:
            repo=RadarRepository(session); await repo.ensure_target(source="reddit", target_id=target.subreddit, name=target.name)
            for item in response.json().get("data",{}).get("children",[]):
                post=item.get("data",{}); external_id=str(post.get("id", ""))
                if not external_id or await repo.message_exists(source="reddit",target_id=target.subreddit,external_id=external_id): continue
                await repo.save_raw_message(RawMessageInput(source="reddit",source_name=target.name,source_target_id=target.subreddit,external_id=external_id,author_id=post.get("author_fullname"),author_username=post.get("author"),published_at=datetime.fromtimestamp(float(post.get("created_utc",0)),timezone.utc),url="https://www.reddit.com"+str(post.get("permalink", "")),title=post.get("title"),text=(post.get("title","")+"\n"+post.get("selftext","")).strip(),reply_count=int(post.get("num_comments",0)),metadata={"subreddit":target.subreddit}))
            await session.commit()
