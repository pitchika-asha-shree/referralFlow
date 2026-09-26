# ReferralFlow

**Healthcare referral intake automation.** Faxed and PDF referrals come in; validated, structured data goes out to each clinic's system as a signed webhook. Anything the system isn't sure about goes to a person, with a ticket and a full audit trail.

```
 fax / PDF / image          ┌──────────── per-client YAML config ────────────┐
        │                   │ required fields · confidence threshold ·       │
        ▼                   │ routing rules · field mapping · webhook        │
 ┌──────────────┐  ┌────────┴─────┐  ┌────────────┐  ┌───────────┐  ┌────────────────┐
 │ text          │─►│ extraction    │─►│ validation │─►│ routing    │─►│ webhook outbox │─► client EHR
 │ pdfplumber /  │  │ labels, prose │  │ NPI, ICD-10│  │ first rule │  │ HMAC, retries, │
 │ OCR fallback  │  │ (+ LLM gaps)  │  │ dates ...  │  │ that wins  │  │ idempotency    │
 └──────────────┘  └──────────────┘  └─────┬──────┘  └───────────┘  └───────┬────────┘
                                           │ blocking issues                  │ retries exhausted / 4xx
                                           ▼                                  ▼
                                   needs_review + ticket  ◄── human fixes ── delivery_failed + ticket
```

All sample data is synthetic. No real patient information is used anywhere.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
# OCR for scanned faxes (optional but recommended):
#   macOS: brew install poppler tesseract    Ubuntu: sudo apt install poppler-utils tesseract-ocr

python scripts/generate_samples.py     # 6 synthetic referrals, incl. a scanned fax
python scripts/demo.py                 # full story in ~2s, no server needed
pytest                                 # test suite
```

Run the service with a mock EHR that fails each delivery once, so you can watch retries:

```bash
python scripts/mock_ehr.py --fail-first 1        # terminal 1
uvicorn app.api.main:app --reload                # terminal 2
open http://localhost:8000        # ops dashboard
open http://localhost:8000/docs   # interactive API docs
```

Or with Docker: `docker compose up --build`.

CLI for operators:

```bash
python -m app.cli lint-configs
python -m app.cli ingest acme_ortho samples/*.pdf
python -m app.cli list --status needs_review
python -m app.cli retry
python -m app.cli tickets
```

## What the demo shows

| Sample | What happens |
|---|---|
| `acme_clean_form.pdf` | All fields found, routed by ICD-10 code to `joint-replacement`, delivered |
| `acme_urgent_spine.pdf` | Different form template; "URGENT" wins over the spine rule, goes to `ortho-urgent` at high priority |
| `acme_referral_letter.pdf` | Free-text letter with no labels; narrative patterns extract the fields, routed to `spine-clinic` |
| `acme_missing_info.pdf` | No member ID and an NPI with a bad check digit, so it goes to `needs_review` with a ticket; a reviewer fixes it and it's delivered |
| `acme_scanned_fax.pdf` | Image-only, skewed, speckled fax; the OCR path reads it and it's delivered |
| `sunrise_cpap_order.pdf` | A second client with different rules: Medicare DME order goes to `medicare-documentation` |
| Same fax sent twice | Detected by SHA-256 and returns the existing referral; nothing is delivered twice |

## Onboarding a new client = one YAML file

```yaml
client_id: acme_ortho
required_fields: [patient_name, patient_dob, insurance_member_id, referring_npi, diagnosis_codes]
min_confidence: 0.8
webhook:
  url: ${EHR_BASE_URL}/webhooks/acme_ortho
  secret_env: ACME_ORTHO_WEBHOOK_SECRET      # secrets live in env vars, never in git
routing:
  rules:
    - name: urgent referral
      when: { field: urgency, op: equals, value: urgent }
      queue: ortho-urgent
      priority: high
    - name: spine
      when: { field: diagnosis_codes, op: starts_with, value: [M48, M50, M51, M54] }
      queue: spine-clinic
  default: { queue: ortho-general }
field_map:                                    # match the client's EHR vocabulary
  patient_name: patientName
  insurance_member_id: memberId
```

Conditions support `equals, not_equals, in, contains, starts_with, exists, missing`, which can be combined with `all`, `any` and `not`. The linter (`python -m app.cli lint-configs`, also run in CI) rejects unknown fields, unknown operators and missing values, so a typo fails the build instead of misrouting patients. `POST /v1/clients/reload` picks up changes without a restart; if the new file is broken, the old config stays live.

## Design decisions

**Precision over recall.** Every extracted field carries a confidence score: 0.95 when it sits next to a label, 0.80 when it comes from a sentence pattern. Each client sets its own threshold. There is deliberately no "take any phone number" fallback, because on a fax cover sheet that is usually the sender's fax line. A blank field that a person fills in is cheap; wrong data inside an EHR is expensive.

**Domain validation, not just regex.** NPIs are checked with the CMS Luhn check digit (prefix 80840), which catches most OCR digit errors. ICD-10 codes are format-checked and normalized (`M545` becomes `M54.5`). Dates are normalized to ISO, and two-digit birth years are resolved into the past. Normalizers are idempotent, so re-validating after a human edit is safe.

**Transactional outbox for webhooks.** A delivery is written to the `deliveries` table and a worker sends it, so it survives crashes and restarts.
- Every attempt carries the same `Idempotency-Key`, so receivers can deduplicate.
- 5xx responses, timeouts, 408 and 429 are retried with exponential backoff and jitter.
- 400, 401, 403, 404 and 422 are not retried, because retrying can't fix bad credentials or bad data. These open a ticket for a person instead.

**Signed webhooks both ways.** `X-ReferralFlow-Signature: t=<ts>,v1=<HMAC-SHA256(secret, "ts.body")>`. Signing the timestamp lets receivers reject replays, and signatures are compared in constant time. The inbound fax-provider endpoint verifies the same scheme.

**Explicit state machine and audit trail.** Status changes go through `check_transition`, so an illegal jump raises an error instead of silently corrupting data. Every change is appended to `referral_events` with the actor and a note, which matters in healthcare.

**Tickets deduplicate.** There is one open ticket per referral per problem, updated in place. Tickets are resolved automatically when a review or redelivery fixes the problem.

**LLM as a fallback, not the foundation.** With `LLM_FALLBACK=on`, only the fields the rules missed are sent to the model, and those results get a lower confidence (0.75). An LLM outage never breaks intake. Real patient data must not be sent to any provider without a signed HIPAA BAA.

**Testable by construction.** The clock, HTTP transport, randomness and text extraction are all injected, so the whole pipeline, including retries hours apart, runs in milliseconds in tests.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/clients/{client_id}/referrals` | Upload a document (201 new, 200 duplicate, 415 unsupported type) |
| POST | `/v1/inbound/fax` | Signed webhook from an e-fax provider (base64 document) |
| GET | `/v1/referrals?status=&client_id=` | List referrals |
| GET | `/v1/referrals/{id}` | Detail with fields, issues, audit trail, tickets and deliveries |
| POST | `/v1/referrals/{id}/review` | Human corrections, then re-validate, re-route and deliver |
| POST | `/v1/referrals/{id}/redeliver` | Retry after the client fixes their endpoint |
| GET / POST | `/v1/tickets`, `/v1/tickets/{id}/resolve` | Incident queue |
| GET / POST | `/v1/clients`, `/v1/clients/reload` | Client configs |
| POST | `/v1/deliveries/process` | Run the outbox once (the background worker also does this) |
| GET | `/v1/stats`, `/healthz` | Operations |

## Project layout

```
app/
  ingest.py            text layer + OCR fallback (pdftoppm + tesseract)
  extraction/rules.py  label/value and narrative extraction with confidence
  extraction/llm.py    optional LLM gap-filler + hybrid extractor
  validation.py        NPI, ICD-10, dates, phones, names; issue severity
  routing.py           declarative rules engine
  client_config.py     YAML loading and linting
  webhooks.py          signing, verification, retry classification, backoff
  pipeline.py          orchestration and state machine
  repository.py, db.py SQLite storage (outbox, tickets, audit log)
  api/                 FastAPI app; templates/ holds the ops dashboard
scripts/               sample generator, mock EHR, end-to-end demo
configs/clients/       one YAML per client
tests/                 unit, pipeline, integration (real PDFs + OCR), API
```

## Production next steps

- **Storage and workers:** move to Postgres and claim deliveries with `FOR UPDATE SKIP LOCKED` so several workers can run at once.
- **Documents:** keep the originals in encrypted object storage (S3 with KMS).
- **Security:** add API authentication and per-client access control, and scrub patient data from logs.
- **Operations:** add metrics per client (auto-delivery rate, review rate, time to delivery) and alert on ticket spikes.
- **Extraction quality:** use layout-aware extraction for complex forms, and build a feedback loop where reviewer corrections become test cases.
