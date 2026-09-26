"""HTTP API + ops dashboard.

Run:  uvicorn app.api.main:app --reload     (docs at http://localhost:8000/docs)

Endpoints are sync `def` on purpose: OCR and outbound HTTP block, and FastAPI runs
sync endpoints in a threadpool so they never stall the event loop.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.concurrency import run_in_threadpool
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.api.schemas import InboundFax, RedeliverRequest, ResolveTicketRequest, ReviewRequest
from app.client_config import ConfigError, load_client_configs
from app.db import connect
from app.extraction.base import CANONICAL_FIELDS
from app.extraction.llm import build_extractor
from app.ingest import UnsupportedDocument
from app.models import TicketStatus, utcnow
from app.pipeline import ActionNotAllowed, IntakePipeline, NotFound, UnknownClient
from app.repository import Repository
from app.settings import get_settings
from app.webhooks import SIGNATURE_HEADER, verify

log = logging.getLogger("referralflow")
settings = get_settings()
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
templates = Environment(
    loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "templates")),
    autoescape=select_autoescape(["html"]),
)


async def _outbox_worker(app: FastAPI) -> None:
    """Retries due webhooks every RETRY_INTERVAL_SECONDS."""
    while True:
        await asyncio.sleep(settings.retry_interval_seconds)
        try:
            outcomes = await run_in_threadpool(_process_outbox, app)
            if outcomes:
                log.info("outbox: %s", [(o.referral_id, o.outcome) for o in outcomes])
        except Exception:  # keep the loop alive no matter what
            log.exception("outbox worker iteration failed")


def _process_outbox(app: FastAPI):
    conn = connect(settings.database_path)
    try:
        return _build_pipeline(app, conn).process_due_deliveries()
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.configs = load_client_configs(settings.configs_dir)
    app.state.extractor = build_extractor()
    connect(settings.database_path).close()       # create schema up front
    task = asyncio.create_task(_outbox_worker(app))
    log.info("loaded clients: %s", ", ".join(app.state.configs))
    yield
    task.cancel()


app = FastAPI(
    title="ReferralFlow",
    version="1.0.0",
    description="Automates healthcare referral intake: document -> fields -> validation -> "
                "routing -> signed webhook into the client's system.",
    lifespan=lifespan,
)


# ------------------------------------------------------------------ plumbing
def _build_pipeline(app: FastAPI, conn) -> IntakePipeline:
    return IntakePipeline(Repository(conn), app.state.configs, extractor=app.state.extractor)


def get_pipeline(request: Request) -> Iterator[IntakePipeline]:
    conn = connect(settings.database_path)      # one connection per request (SQLite + WAL)
    try:
        yield _build_pipeline(request.app, conn)
    finally:
        conn.close()


def _error(status: int):
    async def handler(request: Request, exc: Exception):
        return JSONResponse(status_code=status, content={"detail": str(exc)})
    return handler


app.add_exception_handler(NotFound, _error(404))
app.add_exception_handler(UnknownClient, _error(404))
app.add_exception_handler(ActionNotAllowed, _error(409))
app.add_exception_handler(UnsupportedDocument, _error(415))
app.add_exception_handler(ValueError, _error(422))


def _detail(p: IntakePipeline, referral_id: str) -> dict:
    r = p.repo.get_referral(referral_id)
    if r is None:
        raise NotFound(f"referral '{referral_id}' not found")
    return {
        **r.to_dict(include_text=True),
        "events": p.repo.list_events(r.id),
        "tickets": [t.to_dict() for t in p.repo.list_tickets(referral_id=r.id)],
        "deliveries": [d.to_dict() for d in p.repo.list_deliveries(r.id)],
    }


# ------------------------------------------------------------------ intake
@app.post("/v1/clients/{client_id}/referrals", status_code=201, tags=["intake"])
def upload_referral(client_id: str, response: Response, file: UploadFile = File(...),
                    p: IntakePipeline = Depends(get_pipeline)):
    """Upload a referral document (PDF, scanned fax, image or .txt)."""
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if not content:
        raise HTTPException(400, "empty file")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file larger than 20 MB")
    result = p.ingest(client_id, file.filename or "upload.pdf", content, actor="api")
    if result.duplicate:
        response.status_code = 200
    return {"duplicate": result.duplicate, "referral": result.referral.to_dict()}


@app.post("/v1/inbound/fax", status_code=201, tags=["intake"])
async def inbound_fax(request: Request, response: Response):
    """Webhook receiver for an e-fax provider. Same HMAC scheme we use outbound."""
    body = await request.body()
    if not verify(settings.inbound_secret, body, request.headers.get(SIGNATURE_HEADER)):
        raise HTTPException(401, "invalid signature")
    try:
        msg = InboundFax.model_validate_json(body)
        content = base64.b64decode(msg.content_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, f"invalid payload: {exc}") from exc

    def work():
        conn = connect(settings.database_path)
        try:
            return _build_pipeline(request.app, conn).ingest(
                msg.client_id, msg.filename, content, actor="fax-webhook")
        finally:
            conn.close()

    result = await run_in_threadpool(work)
    if result.duplicate:
        response.status_code = 200
    return {"duplicate": result.duplicate, "referral_id": result.referral.id,
            "status": result.referral.status.value}


# --------------------------------------------------------------- referrals
@app.get("/v1/referrals", tags=["referrals"])
def list_referrals(status: str | None = None, client_id: str | None = None,
                   limit: int = Query(100, le=500), p: IntakePipeline = Depends(get_pipeline)):
    return [r.to_dict() for r in p.repo.list_referrals(status, client_id, limit)]


@app.get("/v1/referrals/{referral_id}", tags=["referrals"])
def get_referral(referral_id: str, p: IntakePipeline = Depends(get_pipeline)):
    return _detail(p, referral_id)


@app.post("/v1/referrals/{referral_id}/review", tags=["referrals"])
def review_referral(referral_id: str, body: ReviewRequest,
                    p: IntakePipeline = Depends(get_pipeline)):
    """Human-in-the-loop correction. Re-validates, re-routes and delivers if clean."""
    p.review(referral_id, body.corrections, reviewer=body.reviewer)
    return _detail(p, referral_id)


@app.post("/v1/referrals/{referral_id}/redeliver", tags=["referrals"])
def redeliver_referral(referral_id: str, body: RedeliverRequest,
                       p: IntakePipeline = Depends(get_pipeline)):
    p.redeliver(referral_id, actor=body.actor)
    return _detail(p, referral_id)


# ----------------------------------------------------------------- tickets
@app.get("/v1/tickets", tags=["tickets"])
def list_tickets(status: str | None = "open", p: IntakePipeline = Depends(get_pipeline)):
    return [t.to_dict() for t in p.repo.list_tickets(status=status)]


@app.post("/v1/tickets/{ticket_id}/resolve", tags=["tickets"])
def resolve_ticket(ticket_id: str, body: ResolveTicketRequest,
                   p: IntakePipeline = Depends(get_pipeline)):
    t = p.repo.get_ticket(ticket_id)
    if t is None:
        raise NotFound(f"ticket '{ticket_id}' not found")
    t.status, t.resolved_at = TicketStatus.RESOLVED, utcnow()
    t.resolution_note = f"{body.actor}: {body.note}"
    p.repo.update_ticket(t)
    return t.to_dict()


# ------------------------------------------------------------------- admin
@app.get("/v1/clients", tags=["admin"])
def list_clients(request: Request):
    return [c.summary() for c in request.app.state.configs.values()]


@app.post("/v1/clients/reload", tags=["admin"])
def reload_clients(request: Request):
    """Pick up YAML changes without a restart. A broken file keeps the old config live."""
    try:
        request.app.state.configs = load_client_configs(settings.configs_dir)
    except ConfigError as exc:
        raise HTTPException(422, f"config not reloaded: {exc}") from exc
    return {"clients": sorted(request.app.state.configs)}


@app.post("/v1/deliveries/process", tags=["admin"])
def process_deliveries(p: IntakePipeline = Depends(get_pipeline)):
    return [o.__dict__ for o in p.process_due_deliveries()]


@app.get("/v1/stats", tags=["admin"])
def stats(p: IntakePipeline = Depends(get_pipeline)):
    return {"referrals_by_status": p.repo.status_counts(),
            "open_tickets": len(p.repo.list_tickets(status="open"))}


@app.get("/healthz", tags=["admin"])
def healthz():
    return {"ok": True}


# --------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request, status: str | None = None,
              p: IntakePipeline = Depends(get_pipeline)):
    html = templates.get_template("dashboard.html").render(
        referrals=[r.to_dict() for r in p.repo.list_referrals(status=status, limit=200)],
        counts=p.repo.status_counts(),
        tickets=[t.to_dict() for t in p.repo.list_tickets(status="open")],
        clients=sorted(request.app.state.configs),
        active_status=status,
    )
    return HTMLResponse(html)


@app.get("/referrals/{referral_id}", response_class=HTMLResponse, include_in_schema=False)
def referral_page(referral_id: str, p: IntakePipeline = Depends(get_pipeline)):
    detail = _detail(p, referral_id)
    return HTMLResponse(templates.get_template("referral.html").render(
        r=detail, field_order=CANONICAL_FIELDS, payload_preview=json.dumps(
            detail["deliveries"][0]["payload"], indent=2) if detail["deliveries"] else None))
