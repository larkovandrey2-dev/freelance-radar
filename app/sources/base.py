from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RawMessageInput(BaseModel):
    source: str
    source_name: str
    source_target_id: str
    external_id: str
    author_id: str | None = None
    author_name: str | None = None
    author_username: str | None = None
    published_at: datetime
    url: str | None = None
    text: str
    title: str | None = None
    reply_count: int | None = None
    view_count: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

