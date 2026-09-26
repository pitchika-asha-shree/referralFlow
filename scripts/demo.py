"""Run the whole story in ~2 seconds with no server setup:

    python scripts/generate_samples.py && python scripts/demo.py

1. Starts a mock EHR that fails the first attempt of every delivery (503).
2. Ingests all sample referrals for two clients.
3. Fast-forwards a fake clock so the retry worker redelivers.
4. A reviewer fixes the referral that was missing data.
5. The same fax arrives again and is recognised as a duplicate.
"""
from __future__ import annotations

import sys
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import os  # noqa: E402

PORT = 9123
os.environ["EHR_BASE_URL"] = f"http://127.0.0.1:{PORT}"

from app.client_config import load_client_configs  # noqa: E402
from app.db import connect  # noqa: E402
from app.models import utcnow  # noqa: E402
from app.pipeline import IntakePipeline  # noqa: E402
from app.repository import Repository  # noqa: E402
from mock_ehr import EHRState, make_server  # noqa: E402

SAMPLES = {
    "acme_ortho": ["acme_clean_form.pdf", "acme_urgent_spine.pdf", "acme_referral_letter.pdf",
                   "acme_missing_info.pdf", "acme_scanned_fax.pdf"],
    "sunrise_dme": ["sunrise_cpap_order.pdf"],
}


class FakeClock:
    def __init__(self):
        self.t = utcnow()

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def table(repo: Repository) -> None:
    print(f"  {'file':28} {'status':16} {'queue':24} priority")
    for r in sorted(repo.list_referrals(), key=lambda r: r.filename):
        print(f"  {r.filename:28} {r.status.value:16} {r.queue or '-':24} {r.priority or '-'}")


def main() -> None:
    missing = [f for fs in SAMPLES.values() for f in fs if not (ROOT / "samples" / f).exists()]
    if missing:
        sys.exit(f"missing samples {missing}; run: python scripts/generate_samples.py")

    ehr_state = EHRState(fail_first=1)
    server = make_server(PORT, ehr_state)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    clock = FakeClock()
    with tempfile.TemporaryDirectory() as tmp:
        repo = Repository(connect(Path(tmp) / "demo.db"))
        pipeline = IntakePipeline(repo, load_client_configs(ROOT / "configs" / "clients"),
                                  clock=clock, rand=lambda: 0.0)

        print("\n1) Ingest (mock EHR returns 503 on each first attempt)")
        for client, files in SAMPLES.items():
            for f in files:
                pipeline.ingest(client, f, (ROOT / "samples" / f).read_bytes(), actor="demo")
        table(repo)

        print("\n2) Fast-forward 2 minutes; the outbox worker retries")
        clock.advance(minutes=2)
        for o in pipeline.process_due_deliveries():
            print(f"  {o.referral_id} -> {o.outcome} (HTTP {o.status_code})")
        table(repo)

        print("\n3) Open tickets")
        for t in repo.list_tickets(status="open"):
            print(f"  [{t.category.value}] {t.summary}\n    " + t.details.replace("\n", "\n    "))

        print("\n4) Reviewer supplies the missing member ID and the correct NPI")
        stuck = repo.list_referrals(status="needs_review")[0]
        r = pipeline.review(stuck.id, {"insurance_member_id": "CIG-5521907",
                                       "referring_npi": "1234567893"}, reviewer="intake-reviewer")
        print(f"  {r.filename}: {r.status.value} (queue={r.queue})")
        clock.advance(minutes=2)
        pipeline.process_due_deliveries()
        print(f"  after retry: {repo.get_referral(r.id).status.value}")

        print("\n5) The clean form is faxed a second time")
        again = pipeline.ingest("acme_ortho", "acme_clean_form.pdf",
                                (ROOT / "samples" / "acme_clean_form.pdf").read_bytes())
        print(f"  duplicate={again.duplicate} -> existing referral {again.referral.id}")

        print("\n6) Audit trail for the reviewed referral")
        for e in repo.list_events(r.id):
            print(f"  {e['actor']:7} {str(e['from_status']):14} -> {str(e['to_status']):16} {e['note']}")

        print(f"\nMock EHR accepted {len(ehr_state.received)} unique referral(s); "
              f"status counts: {repo.status_counts()}")
    server.shutdown()


if __name__ == "__main__":
    main()
