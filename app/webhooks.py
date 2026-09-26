"""Outbound webhooks: payload shaping, HMAC signing, and the retry policy.

Signature scheme (same idea as Stripe):
    X-ReferralFlow-Signature: t=<unix ts>,v1=<hex HMAC-SHA256(secret, f"{t}.{body}")>
Signing the timestamp together with the body lets receivers reject replays.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Protocol

SIGNATURE_HEADER = "X-ReferralFlow-Signature"
IDEMPOTENCY_HEADER = "Idempotency-Key"
DEFAULT_TOLERANCE_SECONDS = 300


# --------------------------------------------------------------------- signing
def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(secret: str, body: bytes, header: str | None,
           tolerance: int = DEFAULT_TOLERANCE_SECONDS, now: int | None = None) -> bool:
    """Receiver-side check. Constant-time compare + replay window."""
    if not header:
        return False
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
        received = parts["v1"]
    except (ValueError, KeyError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


# ------------------------------------------------------------------- transport
@dataclass
class TransportResult:
    status_code: int | None      # None = network error / timeout
    error: str | None = None


class Transport(Protocol):
    def __call__(self, url: str, body: bytes, headers: dict[str, str],
                 timeout: float) -> TransportResult: ...


def urllib_transport(url: str, body: bytes, headers: dict[str, str],
                     timeout: float) -> TransportResult:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return TransportResult(resp.status)
    except urllib.error.HTTPError as exc:
        return TransportResult(exc.code, f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return TransportResult(None, f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}")


# ---------------------------------------------------------------- retry policy
RETRYABLE_4XX = {408, 409, 425, 429}


def classify(result: TransportResult) -> str:
    """-> 'success' | 'retry' | 'permanent'"""
    code = result.status_code
    if code is not None and 200 <= code < 300:
        return "success"
    if code is None or code >= 500 or code in RETRYABLE_4XX:
        return "retry"
    return "permanent"   # 400/401/403/404/422: retrying won't help, a human must fix config/data


def backoff(attempt: int, base_seconds: float = 30, cap_seconds: float = 3600,
            rand: Callable[[], float] = random.random) -> timedelta:
    """Exponential backoff with 'full jitter' so retries from an outage don't stampede."""
    ceiling = min(cap_seconds, base_seconds * (2 ** (attempt - 1)))
    return timedelta(seconds=ceiling * (0.5 + rand() / 2))


# ------------------------------------------------------------------- payloads
def build_payload(referral, field_map: dict[str, str]) -> dict:
    """Canonical fields -> the client's EHR vocabulary."""
    data = {field_map.get(k, k): v for k, v in referral.field_values().items()}
    return {
        "event": "referral.ready",
        "referral_id": referral.id,
        "client_id": referral.client_id,
        "queue": referral.queue,
        "priority": referral.priority,
        "source_document": {"filename": referral.filename, "sha256": referral.document_sha256},
        "data": data,
    }


def encode(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
