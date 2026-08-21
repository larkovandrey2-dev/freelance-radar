"""Cheap, explainable candidate selection before any model request."""
from __future__ import annotations

from dataclasses import dataclass

from app.pipeline.normalize import normalize_text

PURCHASE_STRONG = ("ищу разработчика", "ищем разработчика", "ищу специалиста", "ищем специалиста",
    "ищу подрядчика", "ищем подрядчика", "ищу исполнителя", "нужен разработчик", "нужен программист",
    "нужен специалист", "нужен эксперт", "нужна помощь", "кто может сделать", "кто может реализовать",
    "кто возьмется", "готов заплатить", "готов оплатить", "оплачиваемый проект", "оплачиваемая задача",
    "срочно нужен", "ищу на проект", "нужно собрать", "нужно реализовать", "нужно сделать",
    "нужно разработать", "нужно настроить", "нужно интегрировать", "нужно автоматизировать",
    "нужно починить", "нужно закончить", "looking for someone", "looking for a developer",
    "looking for developer", "looking for an engineer", "looking for a freelancer", "looking for contractor",
    "looking to hire", "need someone", "need a developer", "need developer", "need an engineer",
    "need an expert", "need help building", "need help with", "can someone build", "can someone help",
    "can anyone help", "who can build", "who can fix", "who can implement", "willing to pay",
    "paid project", "paid task", "paid gig", "need this built", "need this fixed", "need this done",
    "need this automated", "need this finished", "need this integrated")
PURCHASE_WEAK = ("бюджет", "оплата", "заказ", "проект", "подработка", "фриланс", "подряд",
    "разовая задача", "разовый проект", "напишите в лс", "пишите в личку", "пишите цену",
    "жду предложения", "предлагайте цену", "budget", "contract", "contractor", "freelance",
    "freelancer", "hiring", "quote me", "send quote", "send your rate", "dm me", "dm with price")
FIT_STRONG = ("ai agent", "rag", "openai", "claude", "qwen", "gemini", "n8n", "zapier", "webhook",
    "rest api", "oauth", "hubspot", "bitrix", "amocrm", "salesforce", "telegram bot", "discord bot",
    "fastapi", "django", "postgres", "postgresql", "redis", "docker", "deployment", "scraper",
    "scraping", "crawler", "monitoring", "dashboard", "admin panel", "internal tool", "mvp", "saas",
    "cursor", "lovable", "supabase", "firebase", "authentication", "rls", "edge function", "serverless")
FIT_GENERIC = ("ai", "llm", "agent", "make", "workflow", "automation", "automate", "api", "integration",
    "crm", "telegram", "whatsapp", "max", "python", "backend", "vps", "server", "parser", "prototype",
    "bolt", "v0", "auth", "database")
VIBECODE = ("almost finished", "almost done", "need help finishing", "need someone to finish", "finish my app",
    "built with lovable", "built in lovable", "built with bolt", "built in bolt", "built with cursor",
    "cursor generated", "ai generated app", "ai built app", "vibe coded", "vibecoded", "prototype works but",
    "works locally but", "can't deploy", "cannot deploy", "deployment broken", "auth broken", "authentication issue",
    "supabase issue", "rls issue", "database broken", "api doesn't work", "webhook doesn't work",
    "integration broken", "stuck for days", "stuck with this", "tried everything", "почти готово",
    "осталось доделать", "надо доделать", "собрал через cursor", "собрал через lovable", "собрал через bolt",
    "нагенерировал приложение", "не могу задеплоить", "сломалась авторизация", "не работает api",
    "не работает webhook", "застрял", "не могу починить")
AGENCY = ("automation agency", "ai agency", "implementation partner", "technical partner", "delivery partner",
    "white label", "white-label", "overflow", "capacity", "too many clients", "client projects", "ongoing projects",
    "ongoing work", "long term contractor", "long-term contractor", "subcontractor", "implementation support",
    "revenue share", "project pipeline", "агентство", "не хватает разработчиков", "много клиентов",
    "нужен подрядчик", "нужен технический партнер", "на постоянные проекты", "подряд на проекты", "белая метка")
URGENCY = ("urgent", "urgently", "asap", "today", "tonight", "this weekend", "tomorrow", "immediately",
    "launching", "deadline", "blocked", "production issue", "срочно", "сегодня", "до завтра", "к выходным",
    "горит", "запуск завтра", "прод упал", "блокирует запуск")
NEGATIVE = ("for hire", "available for work", "available for hire", "my services", "i offer", "portfolio",
    "hire me", "open to work", "ищу работу", "предлагаю услуги", "возьму заказы", "мое портфолио",
    "готов к работе", "internship", "intern", "senior full-time", "full time only", "onsite only", "курс",
    "обучение", "вебинар", "менторство продаю")

@dataclass(frozen=True)
class PrefilterResult:
    score: int
    signals: dict[str, list[str]]
    @property
    def candidate(self) -> bool:
        return self.score >= 5 or bool(self.signals["vibecode"] or self.signals["agency"])

def _hits(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if term in text]

def evaluate(text: str, title: str | None = None) -> PrefilterResult:
    value = normalize_text(" ".join(filter(None, (title, text))))
    signals = {"purchase_strong": _hits(value, PURCHASE_STRONG), "purchase_weak": _hits(value, PURCHASE_WEAK),
        "fit_strong": _hits(value, FIT_STRONG), "fit_generic": _hits(value, FIT_GENERIC),
        "vibecode": _hits(value, VIBECODE), "agency": _hits(value, AGENCY), "urgency": _hits(value, URGENCY),
        "negative": _hits(value, NEGATIVE)}
    score = (5 * bool(signals["purchase_strong"]) + 2 * bool(signals["purchase_weak"]) +
             2 * bool(signals["fit_strong"]) + bool(signals["fit_generic"]) +
             5 * bool(signals["vibecode"]) + 5 * bool(signals["agency"]) + 2 * bool(signals["urgency"]) -
             8 * bool(signals["negative"]))
    return PrefilterResult(score=score, signals=signals)
