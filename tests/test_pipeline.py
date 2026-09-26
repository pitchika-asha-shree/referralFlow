import json

import pytest

from app.models import DeliveryStatus, ReferralStatus, TicketCategory
from app.pipeline import ActionNotAllowed, UnknownClient
from app.webhooks import IDEMPOTENCY_HEADER, SIGNATURE_HEADER, verify
from tests.helpers import CLEAN_TEXT, make_pipeline

MISSING_TEXT = CLEAN_TEXT.replace("Member ID: W123456789", "").replace("1234567893", "1234567890")


def ingest(p, text=CLEAN_TEXT, name="ref.txt"):
    return p.ingest("acme", name, text.encode()).referral


def test_happy_path_delivers_signed_mapped_payload():
    p, transport, clock = make_pipeline()
    r = ingest(p)
    assert r.status == ReferralStatus.DELIVERED
    assert (r.queue, r.routing_rule) == ("joints", "knee")

    call = transport.calls[0]
    payload = json.loads(call["body"])
    assert payload["data"]["patientName"] == "Jane Doe"          # field_map applied
    assert payload["data"]["memberId"] == "W123456789"
    assert payload["data"]["patient_dob"] == "1962-03-14"        # unmapped keys pass through
    secret = p.configs["acme"].webhook.secret
    assert verify(secret, call["body"], call["headers"][SIGNATURE_HEADER],
                  now=int(clock().timestamp()))
    assert call["headers"][IDEMPOTENCY_HEADER].startswith("dlv_")


def test_urgent_rule_wins_over_diagnosis_rule():
    p, _, _ = make_pipeline()
    r = ingest(p, CLEAN_TEXT.replace("Urgency: Routine", "Urgency: STAT"))
    assert (r.queue, r.priority) == ("urgent", "high")


def test_duplicate_document_is_not_reprocessed():
    p, transport, _ = make_pipeline()
    first = p.ingest("acme", "a.txt", CLEAN_TEXT.encode())
    again = p.ingest("acme", "a-resent.txt", CLEAN_TEXT.encode())
    assert again.duplicate and again.referral.id == first.referral.id
    assert len(transport.calls) == 1


def test_unknown_client():
    p, _, _ = make_pipeline()
    with pytest.raises(UnknownClient):
        p.ingest("nobody", "a.txt", b"x")


def test_missing_data_goes_to_review_then_human_fix_delivers():
    p, transport, _ = make_pipeline()
    r = ingest(p, MISSING_TEXT)
    assert r.status == ReferralStatus.NEEDS_REVIEW
    assert {(i.field, i.code) for i in r.blocking_issues} == {
        ("insurance_member_id", "missing_required"), ("referring_npi", "invalid_format")}
    assert transport.calls == []
    [ticket] = p.repo.list_tickets(status="open")
    assert ticket.category == TicketCategory.NEEDS_REVIEW

    # A partial fix keeps it in review and updates the same ticket instead of opening another.
    r = p.review(r.id, {"referring_npi": "1234567893"}, reviewer="ops")
    assert r.status == ReferralStatus.NEEDS_REVIEW
    assert len(p.repo.list_tickets()) == 1

    r = p.review(r.id, {"insurance_member_id": "cig 5521907"}, reviewer="ops")
    assert r.status == ReferralStatus.DELIVERED
    assert r.fields["insurance_member_id"].value == "CIG5521907"
    assert r.fields["insurance_member_id"].source == "human"
    assert p.repo.list_tickets(status="open") == []
    actors = [e["actor"] for e in p.repo.list_events(r.id)]
    assert actors.count("ops") >= 2 and actors[-1] == "worker"


def test_review_rejects_unknown_fields_and_wrong_states():
    p, _, _ = make_pipeline()
    r = ingest(p, MISSING_TEXT)
    with pytest.raises(ValueError):
        p.review(r.id, {"ssn": "123"}, reviewer="ops")
    delivered = ingest(p, CLEAN_TEXT, "other.txt")
    with pytest.raises(ActionNotAllowed):
        p.review(delivered.id, {"patient_name": "X Y"}, reviewer="ops")


def test_transient_failures_are_retried_with_backoff():
    p, transport, clock = make_pipeline(script=[503, None])
    r = ingest(p)
    assert r.status == ReferralStatus.ROUTED
    [d] = p.repo.list_deliveries(r.id)
    assert d.attempts == 1 and d.last_status_code == 503

    assert p.process_due_deliveries() == []          # not due yet: backoff is 15s at rand=0
    clock.advance(seconds=16)
    [o] = p.process_due_deliveries()
    assert o.outcome == "retry"                      # network error, retry again after 30s
    clock.advance(seconds=31)
    [o] = p.process_due_deliveries()
    assert o.outcome == "success"
    assert p.repo.get_referral(r.id).status == ReferralStatus.DELIVERED
    keys = {c["headers"][IDEMPOTENCY_HEADER] for c in transport.calls}
    assert len(transport.calls) == 3 and len(keys) == 1   # same idempotency key every attempt


def test_exhausted_retries_open_a_ticket_and_redeliver_recovers():
    p, transport, clock = make_pipeline(script=[500, 500, 500])
    r = ingest(p)
    for _ in range(3):
        clock.advance(hours=1)
        p.process_due_deliveries()
    r = p.repo.get_referral(r.id)
    assert r.status == ReferralStatus.DELIVERY_FAILED
    [d] = p.repo.list_deliveries(r.id)
    assert d.status == DeliveryStatus.FAILED and d.attempts == 3
    [t] = p.repo.list_tickets(status="open")
    assert t.category == TicketCategory.DELIVERY_FAILURE

    r = p.redeliver(r.id, actor="ops")               # endpoint fixed; transport now returns 200
    assert r.status == ReferralStatus.DELIVERED
    assert p.repo.list_tickets(status="open") == []


def test_permanent_4xx_is_not_retried():
    p, transport, clock = make_pipeline(script=[401])
    r = ingest(p)
    assert r.status == ReferralStatus.DELIVERY_FAILED
    clock.advance(hours=5)
    assert p.process_due_deliveries() == []
    assert len(transport.calls) == 1


def test_unreadable_document_becomes_a_processing_ticket():
    def boom(content, filename):
        raise RuntimeError("corrupt xref table")

    p, _, _ = make_pipeline(text_extractor=boom)
    r = p.ingest("acme", "broken.pdf", b"%PDF-garbage").referral
    assert r.status == ReferralStatus.NEEDS_REVIEW
    [t] = p.repo.list_tickets(status="open")
    assert t.category == TicketCategory.PROCESSING_ERROR and "corrupt" in t.details

    fix = {"patient_name": "Jane Doe", "patient_dob": "1962-03-14", "insurance_member_id": "W1234567",
           "referring_npi": "1234567893", "diagnosis_codes": "M17.11"}
    assert p.review(r.id, fix, reviewer="ops").status == ReferralStatus.DELIVERED
    assert p.repo.list_tickets(status="open") == []
