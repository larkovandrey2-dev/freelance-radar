"""Deterministic offer price and delivery estimates; the LLM only words them."""
from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings


@dataclass(frozen=True)
class Quote:
    price: str
    deadline: str


def _currency(value: str | None) -> str:
    return {"USD": "$", "EUR": "€", "RUB": "₽"}.get((value or "USD").upper(), (value or "USD").upper() + " ")


def calculate_quote(analysis: dict, settings: Settings) -> Quote:
    budget = analysis.get("budget") or {}
    effort = analysis.get("estimated_effort") or {}
    hours = max(0, int(effort.get("max_hours") or effort.get("min_hours") or 0))
    complexity = str(analysis.get("complexity") or "small").lower()
    floor = {"micro": settings.pricing_floor_micro, "small": settings.pricing_floor_small,
             "medium": settings.pricing_floor_medium, "large": settings.pricing_floor_large}.get(complexity, settings.pricing_floor_small)
    minimum, maximum = budget.get("min"), budget.get("max")
    try:
        price = int(minimum) if minimum is not None else (max(floor, min(int(maximum), max(floor, hours * 25))) if maximum is not None else floor)
    except (TypeError, ValueError):
        price = floor
    # Never undercut a configured effort safety floor when an explicit budget is unreasonable.
    if hours:
        price = max(price, min(floor, hours * 25))
    if hours <= 4: deadline = "today"
    elif hours <= 8: deadline = "1 day"
    elif hours <= 16: deadline = "1–2 days"
    elif hours <= 30: deadline = "2–4 days"
    else: deadline = "after a short technical review"
    return Quote(price=f"{_currency(budget.get('currency'))}{price}", deadline=deadline)
