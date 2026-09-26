"""Integration test over the generated sample PDFs (real pdfplumber + OCR)."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.client_config import load_client_configs
from app.db import connect
from app.ingest import ocr_available
from app.pipeline import IntakePipeline
from app.repository import Repository
from tests.helpers import Clock, FakeTransport

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"

EXPECTED = {
    ("acme_ortho", "acme_clean_form.pdf"): ("delivered", "joint-replacement"),
    ("acme_ortho", "acme_urgent_spine.pdf"): ("delivered", "ortho-urgent"),
    ("acme_ortho", "acme_referral_letter.pdf"): ("delivered", "spine-clinic"),
    ("acme_ortho", "acme_missing_info.pdf"): ("needs_review", None),
    ("acme_ortho", "acme_scanned_fax.pdf"): ("delivered", "joint-replacement"),
    ("sunrise_dme", "sunrise_cpap_order.pdf"): ("delivered", "medicare-documentation"),
}


def test_sample_documents_end_to_end():
    pytest.importorskip("pdfplumber")
    pytest.importorskip("reportlab")
    if not all((SAMPLES / f).exists() for _, f in EXPECTED):
        if not shutil.which("pdftoppm"):
            pytest.skip("samples missing and poppler not installed")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_samples.py")], check=True)

    p = IntakePipeline(Repository(connect(":memory:")),
                       load_client_configs(ROOT / "configs" / "clients"),
                       transport=FakeTransport(), clock=Clock())
    for (client, name), (status, queue) in EXPECTED.items():
        if name == "acme_scanned_fax.pdf" and not ocr_available():
            continue
        r = p.ingest(client, name, (SAMPLES / name).read_bytes()).referral
        assert (r.status.value, r.queue) == (status, queue), (name, [i.message for i in r.issues])
