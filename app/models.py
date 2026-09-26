"""Domain models for ReferralFlow.

Plain dataclasses (no ORM) so the core pipeline stays framework-agnostic and
easy to unit test. The API layer converts these to JSON with ``to_dict``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReferralStatus(str, Enum):
    RECEIVED = "received"            # document stored, not yet processed
    NEEDS_REVIEW = "needs_review"    # extraction/validation problems -> human in the loop
    ROUTED = "routed"                # validated, routed to a queue, delivery enqueued
    DELIVERED = "delivered"          # client system acknowledged the webhook (2xx)
    DELIVERY_FAILED = "delivery_failed"  # retries exhausted or permanent 4xx


# Explicit state machine: any transition not listed here is a bug and raises.
ALLOWED_TRANSITIONS: dict[ReferralStatus, set[ReferralStatus]] = {
    ReferralStatus.RECEIVED: {ReferralStatus.NEEDS_REVIEW, ReferralStatus.ROUTED},
    ReferralStatus.NEEDS_REVIEW: {ReferralStatus.NEEDS_REVIEW, ReferralStatus.ROUTED},
    ReferralStatus.ROUTED: {ReferralStatus.DELIVERED, ReferralStatus.DELIVERY_FAILED},
    # A failed delivery is fixed either by a data correction or by a redelivery
    # after the client repairs their endpoint.
    ReferralStatus.DELIVERY_FAILED: {ReferralStatus.ROUTED, ReferralStatus.NEEDS_REVIEW},
    ReferralStatus.DELIVERED: set(),   # terminal
}


class InvalidTransition(Exception):
    pass


def check_transition(current: ReferralStatus, new: ReferralStatus) -> None:
    if new not in ALLOWED_TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"cannot move referral from {current.value} to {new.value}")


class Severity(str, Enum):
    ERROR = "error"      # blocks automatic delivery
    WARNING = "warning"  # delivered, but surfaced on the dashboard


@dataclass
class ExtractedField:
    name: str
    value: Any
    confidence: float
    source: str = "rules"      # rules | llm | human
    raw: str | None = None     # original text before normalization

    @classmethod
    def from_dict(cls, d: dict) -> "ExtractedField":
        return cls(**d)


@dataclass
class ValidationIssue:
    field: str
    code: str        # missing_required | invalid_format | low_confidence
    message: str
    severity: Severity = Severity.ERROR

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ValidationIssue":
        return cls(field=d["field"], code=d["code"], message=d["message"],
                   severity=Severity(d.get("severity", "error")))


@dataclass
class Referral:
    id: str
    client_id: str
    filename: str
    document_sha256: str
    status: ReferralStatus = ReferralStatus.RECEIVED
    text_method: str | None = None          # pdf_text | ocr | plain_text
    raw_text: str = ""
    fields: dict[str, ExtractedField] = field(default_factory=dict)
    issues: list[ValidationIssue] = field(default_factory=list)
    queue: str | None = None
    priority: str | None = None
    routing_rule: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def blocking_issues(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    def field_values(self) -> dict[str, Any]:
        return {name: f.value for name, f in self.fields.items()}

    def to_dict(self, include_text: bool = False) -> dict:
        d = {
            "id": self.id,
            "client_id": self.client_id,
            "filename": self.filename,
            "document_sha256": self.document_sha256,
            "status": self.status.value,
            "text_method": self.text_method,
            "fields": {k: asdict(v) for k, v in self.fields.items()},
            "issues": [i.to_dict() for i in self.issues],
            "queue": self.queue,
            "priority": self.priority,
            "routing_rule": self.routing_rule,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        if include_text:
            d["raw_text"] = self.raw_text
        return d


class TicketStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class TicketCategory(str, Enum):
    NEEDS_REVIEW = "needs_review"
    DELIVERY_FAILURE = "delivery_failure"
    PROCESSING_ERROR = "processing_error"


@dataclass
class Ticket:
    id: str
    referral_id: str
    client_id: str
    category: TicketCategory
    summary: str
    details: str = ""
    status: TicketStatus = TicketStatus.OPEN
    created_at: datetime = field(default_factory=utcnow)
    resolved_at: datetime | None = None
    resolution_note: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "referral_id": self.referral_id,
            "client_id": self.client_id,
            "category": self.category.value,
            "summary": self.summary,
            "details": self.details,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolution_note": self.resolution_note,
        }


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class Delivery:
    """One row in the webhook outbox. Retried by a worker until it succeeds or gives up."""
    id: str
    referral_id: str
    client_id: str
    url: str
    payload: dict
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempts: int = 0
    max_attempts: int = 5
    next_attempt_at: datetime = field(default_factory=utcnow)
    last_status_code: int | None = None
    last_error: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "referral_id": self.referral_id,
            "client_id": self.client_id,
            "url": self.url,
            "payload": self.payload,
            "status": self.status.value,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "next_attempt_at": self.next_attempt_at.isoformat(),
            "last_status_code": self.last_status_code,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
