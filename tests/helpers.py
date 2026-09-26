"""Shared test helpers: an in-memory pipeline with a fake clock and fake HTTP."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.client_config import parse_client_config
from app.db import connect
from app.ingest import TextResult
from app.pipeline import IntakePipeline
from app.repository import Repository
from app.webhooks import TransportResult

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

CLEAN_TEXT = """Sunnyvale Family Medicine
Patient Name: Doe, Jane    DOB: 03/14/1962
Phone: (555) 201-3344
Primary Insurance: Aetna PPO   Member ID: W123456789
Referring Provider: Dr. Alan Smith, MD   NPI: 1234567893
Diagnosis: M17.11 Primary osteoarthritis, right knee
Urgency: Routine
"""

CONFIG = {
    "client_id": "acme",
    "name": "Acme",
    "required_fields": ["patient_name", "patient_dob", "insurance_member_id",
                        "referring_npi", "diagnosis_codes"],
    "min_confidence": 0.8,
    "webhook": {"url": "http://ehr.test/hook", "secret_env": "TEST_ACME_SECRET",
                "max_attempts": 3},
    "routing": {
        "rules": [
            {"name": "urgent", "when": {"field": "urgency", "op": "equals", "value": "urgent"},
             "queue": "urgent", "priority": "high"},
            {"name": "knee", "when": {"field": "diagnosis_codes", "op": "starts_with",
                                      "value": "M17"}, "queue": "joints"},
        ],
        "default": {"queue": "general"},
    },
    "field_map": {"patient_name": "patientName", "insurance_member_id": "memberId"},
}


class Clock:
    def __init__(self, t: datetime = T0):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


class FakeTransport:
    """Replays scripted status codes (None = network error), then answers 200."""

    def __init__(self, script: list[int | None] | None = None):
        self.script = list(script or [])
        self.calls: list[dict] = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": headers})
        code = self.script.pop(0) if self.script else 200
        return TransportResult(code, None if code else "ConnectionError: refused")


def plain_text_extractor(content: bytes, filename: str) -> TextResult:
    return TextResult(content.decode(), "plain_text", 1)


def make_pipeline(script=None, config=None, text_extractor=plain_text_extractor):
    cfg = parse_client_config(config or CONFIG, "test")
    transport, clock = FakeTransport(script), Clock()
    p = IntakePipeline(Repository(connect(":memory:")), {cfg.client_id: cfg},
                       transport=transport, clock=clock, rand=lambda: 0.0,
                       text_extractor=text_extractor)
    return p, transport, clock
