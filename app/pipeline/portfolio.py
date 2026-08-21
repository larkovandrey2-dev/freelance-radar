"""Small deterministic portfolio matcher; no embeddings or hidden claims."""
from __future__ import annotations
from typing import Any

def select_projects(profile: dict[str, Any], *, text: str, analysis: dict[str, Any], limit: int = 2) -> list[dict[str, Any]]:
    haystack = " ".join([text, *map(str, analysis.get("known_components") or []), *map(str, analysis.get("unknown_integrations") or [])]).lower()
    ranked: list[tuple[int, dict[str, Any]]] = []
    for project in profile.get("portfolio", []):
        terms = [*project.get("tags", []), *project.get("strengths", [])]
        score = sum(1 for term in terms if str(term).lower() in haystack)
        if score:
            ranked.append((score, {key: project.get(key) for key in ("id", "name", "tags", "strengths", "description")}))
    return [project for _, project in sorted(ranked, key=lambda item: item[0], reverse=True)[:limit]]
