from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower().replace("ё", "е")
    # Tracking parameters make otherwise identical posts look different to the
    # cache.  Keep meaningful query parameters, only remove common trackers.
    def clean_url(match: re.Match[str]) -> str:
        parts = urlsplit(match.group(0))
        query = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
                 if not key.lower().startswith(("utm_", "fbclid", "gclid", "yclid"))]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    value = re.sub(r"https?://[^\s<>]+", clean_url, value)
    return re.sub(r"\s+", " ", value).strip()


def content_hash(value: str) -> str:
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()
