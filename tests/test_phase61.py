from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.config import Settings
from app.pipeline.lead_worker import LeadWorker
from app.pipeline.portfolio import select_projects
from app.pipeline.prefilter import evaluate
from app.storage.models import RawMessage


class Phase61Tests(unittest.TestCase):
    def test_twenty_freelance_technical_examples_reach_analysis(self):
        examples = [
            "Нужен Python скрипт, бюджет 10 000 рублей", "Нужно сделать Telegram бота, оплата за задачу",
            "Ищу исполнителя: подключить REST API", "Разовая задача: написать парсер сайта",
            "Нужно починить webhook и задеплоить Docker", "Need a Python developer for a CSV processing script",
            "Looking for someone to fix our FastAPI backend", "Paid task: build a small admin panel",
            "Нужен разработчик для PostgreSQL интеграции", "Нужно сделать aiogram бота, бюджет есть",
            "Need help with Google Sheets automation, paid project", "Ищу подрядчика на API integration",
            "Разовый проект: scraping и выгрузка Excel", "Нужно закончить существующий MVP",
            "Contract: small Django bug fixing task", "Требуется backend для небольшого сайта",
            "Нужно настроить CRM webhook", "Freelance: Telegram parser and deployment",
            "Ищу исполнителя для доработки базы данных", "Paid task: Docker VPS setup",
        ]
        for text in examples:
            with self.subTest(text=text):
                self.assertTrue(evaluate(text, source_tags=["soft_filter"]).candidate)

    def test_enterprise_task_is_penalized_and_not_alerted(self):
        worker = LeadWorker(Settings(_env_file=None), None)  # type: ignore[arg-type]
        raw = RawMessage(source="telegram", external_id="1", source_target_id="x", published_at=datetime.now(timezone.utc), raw_text="SAP", normalized_text="sap", content_hash="x", metadata_={"tags": ["freelance"]})
        analysis = worker._enrich_analysis({"relevant": True, "purchase_intent": 9, "fit": 4, "fit_for_user": 3, "delivery_confidence": 3, "task_shape": "TOO_LARGE", "integration_risk": "HIGH", "requires_enterprise_expertise": True, "urgency": 8})
        score = worker._score(analysis, raw)
        self.assertLess(score, 72)
        self.assertFalse(worker._should_alert(analysis, raw, score, 1))

    def test_portfolio_matcher_limits_to_two_relevant_projects(self):
        profile = {"portfolio": [{"id": "radar", "name": "Radar", "tags": ["Telegram", "Telethon"], "strengths": ["parsing"], "description": "x"}, {"id": "desk", "name": "Desk", "tags": ["FastAPI", "API"], "strengths": ["backend"], "description": "x"}, {"id": "other", "name": "Other", "tags": ["voice"], "strengths": [], "description": "x"}]}
        selected = select_projects(profile, text="Need Telegram Telethon parser", analysis={"known_components": ["Telegram", "parser"]})
        self.assertEqual([item["id"] for item in selected], ["radar"])

if __name__ == "__main__":
    unittest.main()
