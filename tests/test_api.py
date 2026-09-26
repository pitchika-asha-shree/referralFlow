"""API smoke tests. Skipped automatically if FastAPI isn't installed."""
import base64
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")


def test_api_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("EHR_BASE_URL", "http://127.0.0.1:1")     # unreachable -> retry path
    monkeypatch.setenv("INBOUND_WEBHOOK_SECRET", "inbound")
    import importlib

    import app.api.main as main
    importlib.reload(main)
    from fastapi.testclient import TestClient

    from app.webhooks import SIGNATURE_HEADER, sign
    from tests.helpers import CLEAN_TEXT

    with TestClient(main.app) as client:
        assert client.get("/healthz").json() == {"ok": True}
        assert {c["client_id"] for c in client.get("/v1/clients").json()} >= {"acme_ortho"}

        res = client.post("/v1/clients/acme_ortho/referrals",
                          files={"file": ("ref.txt", CLEAN_TEXT.encode(), "text/plain")})
        assert res.status_code == 201
        rid = res.json()["referral"]["id"]
        assert res.json()["referral"]["status"] == "routed"      # delivery pending retry

        again = client.post("/v1/clients/acme_ortho/referrals",
                            files={"file": ("ref.txt", CLEAN_TEXT.encode(), "text/plain")})
        assert again.status_code == 200 and again.json()["duplicate"]

        assert client.get(f"/v1/referrals/{rid}").json()["events"]
        assert client.post("/v1/clients/nope/referrals",
                           files={"file": ("a.txt", b"x", "text/plain")}).status_code == 404
        assert client.post("/v1/clients/acme_ortho/referrals",
                           files={"file": ("a.docx", b"x", "application/octet-stream")}).status_code == 415

        body = json.dumps({"client_id": "acme_ortho", "filename": "f.txt",
                           "content_base64": base64.b64encode(b"Patient Name: Al Bo").decode()}).encode()
        assert client.post("/v1/inbound/fax", content=body).status_code == 401
        ok = client.post("/v1/inbound/fax", content=body,
                         headers={SIGNATURE_HEADER: sign("inbound", body)})
        assert ok.status_code == 201 and ok.json()["status"] == "needs_review"

        assert client.get("/").status_code == 200
        assert client.get(f"/referrals/{rid}").status_code == 200
