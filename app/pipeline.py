"""The intake pipeline: document -> text -> fields -> validation -> routing -> webhook.

    received ──► needs_review ──(human fixes)──┐
        │                                      ▼
        └────────────────────────────────► routed ──► delivered
                                               │
                                               └──► delivery_failed ──(redeliver / fix)──► routed

Everything with a side effect (clock, HTTP, randomness, OCR) is injected, so the
whole flow runs in unit tests with no network and a fake clock.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from app.client_config import ClientConfig
from app.extraction.base import CANONICAL_FIELDS
from app.extraction.llm import HybridExtractor
from app.extraction.rules import RuleBasedExtractor
from app.ingest import TextResult, UnsupportedDocument, extract_text
from app.models import (
    Delivery, DeliveryStatus, ExtractedField, Referral, ReferralStatus, Severity, Ticket,
    TicketCategory, TicketStatus, ValidationIssue, check_transition, utcnow,
)
from app.repository import Repository, new_id
from app.routing import route
from app.validation import validate_fields
from app.webhooks import (
    IDEMPOTENCY_HEADER, SIGNATURE_HEADER, Transport, backoff, build_payload, classify, encode,
    sign, urllib_transport,
)

REVIEWABLE = {ReferralStatus.NEEDS_REVIEW, ReferralStatus.DELIVERY_FAILED}


class UnknownClient(Exception):
    pass


class NotFound(Exception):
    pass


class ActionNotAllowed(Exception):
    pass


@dataclass
class IngestResult:
    referral: Referral
    duplicate: bool


@dataclass
class DeliveryOutcome:
    delivery_id: str
    referral_id: str
    outcome: str            # success | retry | permanent | exhausted
    status_code: int | None


class IntakePipeline:
    def __init__(
        self,
        repo: Repository,
        configs: dict[str, ClientConfig],
        extractor=None,
        transport: Transport = urllib_transport,
        clock: Callable[[], datetime] = utcnow,
        rand: Callable[[], float] = random.random,
        text_extractor: Callable[[bytes, str], TextResult] = extract_text,
    ):
        self.repo = repo
        self.configs = configs
        self.extractor = extractor or HybridExtractor(RuleBasedExtractor(), None)
        self.transport = transport
        self.now = clock
        self.rand = rand
        self.text_extractor = text_extractor

    # ================================================================= intake
    def ingest(self, client_id: str, filename: str, content: bytes,
               actor: str = "api") -> IngestResult:
        cfg = self._config(client_id)
        sha = hashlib.sha256(content).hexdigest()
        if (existing := self.repo.find_by_hash(client_id, sha)) is not None:
            # Fax lines resend constantly; the same bytes must never create a second referral.
            return IngestResult(existing, duplicate=True)

        try:
            text = self.text_extractor(content, filename)
            error = None
        except UnsupportedDocument:
            raise
        except Exception as exc:  # corrupt PDF, OCR crash... keep the doc and ask a human
            text, error = TextResult("", "failed", 0), f"{type(exc).__name__}: {exc}"

        now = self.now()
        referral = Referral(id=new_id("ref"), client_id=client_id, filename=filename,
                            document_sha256=sha, text_method=text.method, raw_text=text.text,
                            created_at=now, updated_at=now)
        self.repo.insert_referral(referral)
        self.repo.add_event(referral.id, now, None, ReferralStatus.RECEIVED.value, actor,
                            f"received {filename} ({text.method}, {text.pages} page(s))")

        if error:
            referral.issues = [ValidationIssue("document", "processing_error", error)]
            self._transition(referral, ReferralStatus.NEEDS_REVIEW, actor, "document could not be read")
            self.repo.update_referral(referral)
            self._open_ticket(referral, TicketCategory.PROCESSING_ERROR,
                              f"Could not read {filename}", error)
            return IngestResult(referral, duplicate=False)

        fields = self.extractor.extract(text.text)
        self._validate_and_route(referral, fields, cfg, actor)
        self.process_due_deliveries(referral_id=referral.id)
        return IngestResult(self.repo.get_referral(referral.id), duplicate=False)

    # ============================================================ human review
    def review(self, referral_id: str, corrections: dict[str, Any], reviewer: str) -> Referral:
        referral = self._get(referral_id)
        if referral.status not in REVIEWABLE:
            raise ActionNotAllowed(f"referral is {referral.status.value}; only "
                                   f"{', '.join(s.value for s in REVIEWABLE)} can be reviewed")
        unknown = [k for k in corrections if k not in CANONICAL_FIELDS]
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(unknown)}")

        fields = dict(referral.fields)
        for name, value in corrections.items():
            fields[name] = ExtractedField(name, value, 1.0, "human", str(value))
        self.repo.add_event(referral.id, self.now(), referral.status.value, referral.status.value,
                            reviewer, f"corrected: {', '.join(sorted(corrections)) or 'nothing'}")

        cfg = self._config(referral.client_id)
        self._validate_and_route(referral, fields, cfg, reviewer)
        self.process_due_deliveries(referral_id=referral.id)
        return self.repo.get_referral(referral.id)

    def redeliver(self, referral_id: str, actor: str) -> Referral:
        """Use after the client fixes their endpoint (e.g. a 401 from a rotated secret)."""
        referral = self._get(referral_id)
        if referral.status != ReferralStatus.DELIVERY_FAILED:
            raise ActionNotAllowed("only referrals in delivery_failed can be redelivered")
        cfg = self._config(referral.client_id)
        self._transition(referral, ReferralStatus.ROUTED, actor, "manual redelivery")
        self.repo.update_referral(referral)
        self._enqueue_delivery(referral, cfg)
        self._resolve_tickets(referral.id, TicketCategory.DELIVERY_FAILURE, f"redelivered by {actor}")
        self.process_due_deliveries(referral_id=referral.id)
        return self.repo.get_referral(referral.id)

    # ============================================================== delivery
    def process_due_deliveries(self, referral_id: str | None = None) -> list[DeliveryOutcome]:
        """The outbox worker. Safe to call repeatedly (API background loop, CLI, tests)."""
        outcomes = []
        for d in self.repo.due_deliveries(self.now()):
            if referral_id and d.referral_id != referral_id:
                continue
            outcomes.append(self._attempt(d))
        return outcomes

    def _attempt(self, d: Delivery) -> DeliveryOutcome:
        cfg = self.configs.get(d.client_id)
        body = encode(d.payload)
        now = self.now()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ReferralFlow/1.0",
            IDEMPOTENCY_HEADER: d.id,       # receivers dedupe on this across our retries
        }
        if cfg is None:
            result_code, error, outcome = None, "client configuration was removed", "permanent"
        else:
            headers[SIGNATURE_HEADER] = sign(cfg.webhook.secret, body, int(now.timestamp()))
            result = self.transport(d.url, body, headers, cfg.webhook.timeout_seconds)
            result_code, error, outcome = result.status_code, result.error, classify(result)

        d.attempts += 1
        d.last_status_code, d.last_error, d.updated_at = result_code, error, now
        referral = self._get(d.referral_id)

        if outcome == "success":
            d.status = DeliveryStatus.SUCCEEDED
            self.repo.update_delivery(d)
            self._transition(referral, ReferralStatus.DELIVERED, "worker",
                             f"HTTP {result_code} after {d.attempts} attempt(s)")
            self.repo.update_referral(referral)
            return DeliveryOutcome(d.id, d.referral_id, "success", result_code)

        if outcome == "retry" and d.attempts < d.max_attempts:
            d.next_attempt_at = now + backoff(d.attempts, rand=self.rand)
            self.repo.update_delivery(d)
            self.repo.add_event(referral.id, now, None, None, "worker",
                                f"attempt {d.attempts} failed ({error or result_code}); "
                                f"retry at {d.next_attempt_at.isoformat(timespec='seconds')}")
            return DeliveryOutcome(d.id, d.referral_id, "retry", result_code)

        d.status = DeliveryStatus.FAILED
        self.repo.update_delivery(d)
        reason = ("non-retryable response" if outcome == "permanent"
                  else f"gave up after {d.attempts} attempts")
        self._transition(referral, ReferralStatus.DELIVERY_FAILED, "worker",
                         f"{reason}: {error or result_code}")
        self.repo.update_referral(referral)
        self._open_ticket(
            referral, TicketCategory.DELIVERY_FAILURE,
            f"Webhook to {referral.client_id} failed ({error or result_code})",
            f"{reason}. URL: {d.url}. Delivery {d.id}. Last status: {result_code}. "
            f"Fix the endpoint and use redeliver, or correct the data if the client rejected it.")
        return DeliveryOutcome(d.id, d.referral_id,
                               "permanent" if outcome == "permanent" else "exhausted", result_code)

    # =============================================================== helpers
    def _validate_and_route(self, referral: Referral, fields: dict[str, ExtractedField],
                            cfg: ClientConfig, actor: str) -> None:
        normalized, issues = validate_fields(fields, cfg.required_fields, cfg.min_confidence)
        referral.fields, referral.issues = normalized, issues

        if referral.blocking_issues:
            self._transition(referral, ReferralStatus.NEEDS_REVIEW, actor,
                             f"{len(referral.blocking_issues)} blocking issue(s)")
            self.repo.update_referral(referral)
            lines = [f"- {i.field}: {i.message}" for i in referral.blocking_issues]
            self._open_ticket(referral, TicketCategory.NEEDS_REVIEW,
                              f"{referral.filename}: {len(lines)} field(s) need review",
                              "\n".join(lines))
            return

        decision = route(referral.field_values(), cfg.routing_rules, cfg.default_route)
        referral.queue, referral.priority, referral.routing_rule = (
            decision.queue, decision.priority, decision.rule)
        self._transition(referral, ReferralStatus.ROUTED, actor,
                         f"queue={decision.queue} priority={decision.priority} rule={decision.rule}")
        self.repo.update_referral(referral)
        self._enqueue_delivery(referral, cfg)
        self._resolve_tickets(referral.id, TicketCategory.NEEDS_REVIEW, f"resolved by {actor}")
        self._resolve_tickets(referral.id, TicketCategory.DELIVERY_FAILURE, f"data corrected by {actor}")
        self._resolve_tickets(referral.id, TicketCategory.PROCESSING_ERROR, f"entered manually by {actor}")
        warnings = [i for i in issues if i.severity == Severity.WARNING]
        if warnings:
            self.repo.add_event(referral.id, self.now(), None, None, actor,
                                "warnings: " + "; ".join(f"{w.field} {w.code}" for w in warnings))

    def _enqueue_delivery(self, referral: Referral, cfg: ClientConfig) -> Delivery:
        now = self.now()
        d = Delivery(id=new_id("dlv"), referral_id=referral.id, client_id=referral.client_id,
                     url=cfg.webhook.url, payload=build_payload(referral, cfg.field_map),
                     max_attempts=cfg.webhook.max_attempts, next_attempt_at=now,
                     created_at=now, updated_at=now)
        self.repo.insert_delivery(d)
        return d

    def _transition(self, referral: Referral, new: ReferralStatus, actor: str, note: str) -> None:
        check_transition(referral.status, new)
        old = referral.status
        referral.status, referral.updated_at = new, self.now()
        self.repo.add_event(referral.id, referral.updated_at, old.value, new.value, actor, note)

    def _open_ticket(self, referral: Referral, category: TicketCategory, summary: str,
                     details: str) -> Ticket:
        existing = self.repo.find_open_ticket(referral.id, category)
        if existing:   # one open ticket per problem, updated in place instead of duplicated
            existing.summary, existing.details = summary, details
            self.repo.update_ticket(existing)
            return existing
        t = Ticket(id=new_id("tkt"), referral_id=referral.id, client_id=referral.client_id,
                   category=category, summary=summary, details=details, created_at=self.now())
        self.repo.insert_ticket(t)
        return t

    def _resolve_tickets(self, referral_id: str, category: TicketCategory, note: str) -> None:
        t = self.repo.find_open_ticket(referral_id, category)
        if t:
            t.status, t.resolved_at, t.resolution_note = TicketStatus.RESOLVED, self.now(), note
            self.repo.update_ticket(t)

    def _config(self, client_id: str) -> ClientConfig:
        if client_id not in self.configs:
            raise UnknownClient(f"unknown client '{client_id}'")
        return self.configs[client_id]

    def _get(self, referral_id: str) -> Referral:
        r = self.repo.get_referral(referral_id)
        if r is None:
            raise NotFound(f"referral '{referral_id}' not found")
        return r
