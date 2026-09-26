"""Optional LLM fallback for fields the rules could not find.

Only the *missing or low-confidence* fields are requested, which keeps cost down
and keeps the deterministic extractor as the source of truth when it is confident.

PHI note: never send real patient data to an LLM provider without a signed BAA
(HIPAA Business Associate Agreement). This project only ships synthetic data.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Callable

from app.extraction.base import CANONICAL_FIELDS
from app.models import ExtractedField

LLM_CONFIDENCE = 0.75   # deliberately below LABELED: humans still review if the client is strict

PROMPT = """You extract data from a healthcare referral document.
Return ONLY a JSON object (no markdown) with these keys when present in the document:
{fields}
Rules: copy values exactly as written; diagnosis_codes is a list of ICD-10 codes;
omit a key entirely if the document does not contain it. Never guess.

Document:
<<<
{text}
>>>"""

# (prompt) -> raw model text. Injected so tests never hit the network.
Completion = Callable[[str], str]


def anthropic_completion(api_key: str, model: str) -> Completion:
    def call(prompt: str) -> str:
        body = json.dumps({
            "model": model,
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body, method="POST",
            headers={"content-type": "application/json", "x-api-key": api_key,
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return "".join(b.get("text", "") for b in data.get("content", []))
    return call


class LLMExtractor:
    name = "llm"

    def __init__(self, complete: Completion):
        self.complete = complete

    def extract(self, text: str, only: list[str] | None = None) -> dict[str, ExtractedField]:
        wanted = only or list(CANONICAL_FIELDS)
        raw = self.complete(PROMPT.format(fields=", ".join(wanted), text=text[:15000]))
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return {
            k: ExtractedField(k, v, LLM_CONFIDENCE, "llm", json.dumps(v))
            for k, v in data.items() if k in wanted and v not in (None, "", [])
        }


class HybridExtractor:
    """Rules first; ask the LLM only for fields the rules missed or were unsure about."""
    name = "hybrid"

    def __init__(self, rules, llm: LLMExtractor | None, confidence_floor: float = 0.8):
        self.rules, self.llm, self.floor = rules, llm, confidence_floor

    def extract(self, text: str) -> dict[str, ExtractedField]:
        fields = self.rules.extract(text)
        if self.llm is None:
            return fields
        gaps = [f for f in CANONICAL_FIELDS
                if f not in fields or fields[f].confidence < self.floor]
        if gaps:
            try:
                for name, f in self.llm.extract(text, only=gaps).items():
                    if name not in fields or fields[name].confidence < f.confidence:
                        fields[name] = f
            except Exception:  # an LLM outage must never break intake
                pass
        return fields


def build_extractor():
    from app.extraction.rules import RuleBasedExtractor
    rules = RuleBasedExtractor()
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or os.getenv("LLM_FALLBACK", "off").lower() != "on":
        return HybridExtractor(rules, None)
    model = os.getenv("LLM_MODEL", "claude-sonnet-5")
    return HybridExtractor(rules, LLMExtractor(anthropic_completion(key, model)))
