# DocuReview

An invoice-processing API that extracts structured fields, checks source evidence and totals,
and routes uncertain results to human review. Built with Python, FastAPI, Pydantic, SQLite,
and an optional local Ollama model.

This is a working single-tenant MVP. It includes a deterministic demo parser so you can run
the full upload → validation → review flow before configuring inference. **Demo mode is not AI.**

## What it does

- Accepts raw UTF-8 text or text-based PDF uploads, up to 5 MiB, 10 PDF pages, and 12,000 characters.
- Extracts invoice number, vendor, date, currency, subtotal, tax, and total.
- Requests schema-constrained JSON from Ollama and validates the response locally.
- Checks exact source quotes, field values, supported currencies, dates, and monetary precision.
- Uses decimal arithmetic to require `subtotal + tax == total`.
- Stores original extraction, assessment, final reviewed values, and review history in SQLite.
- Separates upload/read credentials from review credentials.
- Rejects stale or conflicting review submissions with HTTP 409.
- Includes interactive API documentation at `/docs`, tests, a Dockerfile, and GitHub Actions CI.

## Quick start

Requires Python 3.12+. Commands below use Bash; PowerShell equivalents follow.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .

export DOCUREVIEW_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export DOCUREVIEW_REVIEWER_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export DOCUREVIEW_PROVIDER=demo
uvicorn docureview.api:create_app --factory --host 127.0.0.1 --port 8000 --limit-concurrency 8
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
$env:DOCUREVIEW_API_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:DOCUREVIEW_REVIEWER_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:DOCUREVIEW_PROVIDER = "demo"
uvicorn docureview.api:create_app --factory --host 127.0.0.1 --port 8000 --limit-concurrency 8
```

Set the same keys in the client terminal, or use `/docs` and its **Authorize** button.
The `.env.example` file documents configuration; the Python process does **not** automatically
load `.env`. Set environment variables explicitly or pass an env file to Docker.

### Upload an invoice

Send the file bytes directly, not multipart form data. In PowerShell use `curl.exe` and
`$env:DOCUREVIEW_API_KEY` instead of the Bash variable syntax below.

```bash
curl -sS http://127.0.0.1:8000/v1/documents \
  -H "X-API-Key: $DOCUREVIEW_API_KEY" \
  -H 'Content-Type: text/plain' \
  --data-binary @examples/invoice.txt
```

For PDFs, use `Content-Type: application/pdf` and `--data-binary @invoice.pdf`.
The response includes an `id`, extracted fields, per-field checks, validation issues, and status.
With the bundled fixture, the validated total is `1080.00` and the status is `needs_review`.

### Review

```bash
curl -sS http://127.0.0.1:8000/v1/reviews \
  -H "X-API-Key: $DOCUREVIEW_REVIEWER_KEY"

# Replace DOCUMENT_ID with the id returned by upload.
curl -sS http://127.0.0.1:8000/v1/documents/DOCUMENT_ID/review \
  -H "X-API-Key: $DOCUREVIEW_REVIEWER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"expected_version":1,"decision":"approved","note":"Checked against source"}'
```

Approval can include `corrected_invoice` with **all seven fields**. It is required when the
original extraction fails invoice validation. Corrected values must satisfy the same type,
currency, precision, and total checks. Original extraction and assessment remain available.
Reviewers are responsible for comparing corrections with their original source document.
Rejection sets `final_invoice` to null. Completed decisions cannot be reopened in this version.

## Enable AI extraction

Install and run [Ollama](https://docs.ollama.com/), then pull a model that fits your hardware
and supports structured output. Configure the exact installed model tag:

```bash
export DOCUREVIEW_PROVIDER=ollama
export OLLAMA_MODEL='YOUR_INSTALLED_MODEL_TAG'
export OLLAMA_URL=http://127.0.0.1:11434
```

`YOUR_INSTALLED_MODEL_TAG` is a configuration placeholder, not a supplied model. Restart the
API after setting it. See Ollama's [chat API](https://docs.ollama.com/api/chat) for the
`format` JSON-schema parameter used by this implementation. Inference is synchronous per
request, runs outside the ASGI event loop, and has a 120-second HTTP timeout. There are no
automatic inference retries. Provider failures return 502 and do not create a document record.

The model receives document text as untrusted input and is given no tools or external actions.
Prompts are defense in depth, not a guarantee against prompt injection. Validate outputs and
keep human review enabled until you have measured performance on your actual invoices.

## Confidence and review policy

Each field has a provider-reported confidence and an exact supporting quote. Its effective
score becomes zero when the quote is absent from the source or the extracted value is absent
from the quote. Document confidence is the **minimum** effective field score.

These are **uncalibrated review signals**, not probabilities of correctness. An exact quote can
still come from the wrong part of an invoice. Normalized dates and amounts can fail the literal
check even when correct; that conservative behavior sends them to review.

Automatic acceptance is disabled by default. When explicitly enabled with
`DOCUREVIEW_AUTO_ACCEPT=true`, the Ollama path accepts only if all checks pass and every field
meets `DOCUREVIEW_REVIEW_THRESHOLD` (default 0.90). Demo mode always requires review. A zero
threshold does not bypass missing-field, evidence, or invoice validation checks.

Statuses: `needs_review`, `auto_accepted`, `approved`, `rejected`. Only `auto_accepted` and
`approved` documents have a `final_invoice` suitable for downstream consumption.

## API

| Method | Route | Access | Purpose |
| --- | --- | --- | --- |
| GET | `/healthz` | Public | Process liveness only; does not check database or model readiness |
| POST | `/v1/documents` | Upload or reviewer key | Process raw document bytes |
| GET | `/v1/documents/{id}` | Upload or reviewer key | Read extraction and review record |
| GET | `/v1/reviews?limit=20&offset=0` | Reviewer key | List pending documents |
| POST | `/v1/documents/{id}/review` | Reviewer key | Approve with optional correction, or reject |

Error statuses: 401 invalid credential, 403 wrong role, 404 missing document, 409 stale or
completed review, 413 oversized upload, 415 unsupported content type, 422 invalid document
or review, 502 inference failure, 503 database unavailable. API errors omit submitted values.
Offset pagination can shift as other reviewers consume the queue; refresh from offset 0.

## Run checks

```bash
ruff check .
ruff format --check .
pytest -q
```

Tests cover the API workflow, persistence, role separation, upload limits, malformed and
encrypted PDFs, blank scanned-style PDFs, invalid totals, evidence checks, provider response
handling, and competing review decisions. Model transport tests use `httpx.MockTransport`;
they do **not** demonstrate live model accuracy. GitHub CI uses the same commands.

The lock files pin the versions used during local verification. `requirements.lock` contains
runtime dependencies; `requirements-dev.lock` adds test/lint tools. These are version pins,
not hash-verified supply-chain locks. Review and update them alongside `pyproject.toml`.

## Docker

```bash
cp .env.example .env
# Edit .env and set two distinct generated keys before starting.
docker build -t docureview .
docker run --rm -p 127.0.0.1:8000:8000 --env-file .env \
  -v docureview-data:/app/data docureview
```

The container runs as a non-root user. A named volume retains SQLite data. A host Ollama server
is not reachable through the container's `127.0.0.1`; configure a reachable trusted endpoint.
For Docker Desktop this is commonly `http://host.docker.internal:11434`, subject to the host's
Ollama bind settings. Do not expose an unauthenticated model endpoint to the public internet.

## Scope and operational limits

- **Invoice headers only:** no line items, discounts, shipping, multiple tax rates, credit notes,
  multiple invoices per file, or documents whose total formula differs from subtotal plus tax.
  Supported currencies are USD, EUR, GBP, INR, CAD, and AUD, with two-decimal precision.
- **No OCR:** scans and PDF pages without readable text are rejected. Mixed image/text documents
  can still contain invisible-to-parser information; inspect the original before approval.
- **Single tenant:** all authorized readers share the same dataset. One reviewer key maps to a
  configured reviewer name. For teams, add OIDC identities, per-document authorization, and a
  durable audit system; the SQLite review record is not tamper-proof.
- **Local resource limits:** byte, page, and text caps reduce accidental oversized inputs but do
  not fully protect against malicious compressed PDFs. Before accepting untrusted public
  uploads, isolate parsing in CPU/memory-limited worker processes and enforce ingress body,
  rate, concurrency, and request-deadline limits. The parser has no hard execution deadline.
- **Synchronous processing:** no job queue, cancellation guarantee, idempotency key, or automatic
  retry. Retrying a successful upload creates a second record; SHA-256 is recorded for reference,
  not deduplication. SQLite is intended for a single host with modest write traffic.
- **Data handling:** the application does not persist raw files or full source text, but extracted
  fields, quotes, corrections, and notes can contain sensitive information. Use restricted data
  directory permissions, encrypted storage, backups, and a defined retention/deletion policy.
  A configured model endpoint receives the full extracted text; assess its data handling.
- **Deployment:** place behind TLS and a gateway before remote access. Keep keys out of Git and
  logs. `/docs` and OpenAPI schemas are public but data endpoints require authentication.

## Evaluate before enabling automatic acceptance

Build a labeled evaluation set split by vendor and template, including ambiguous dates,
missing tax, duplicated totals, OCR noise, unsupported currencies, and injected instructions.
Avoid putting real sensitive invoices in Git. Report per-field exact-match accuracy, full-invoice
accuracy, false acceptance rate, review rate, p50/p95 latency, and inference cost or resource use.
Evaluate auto-accepted errors separately from overall extraction accuracy. Select a review
threshold against your acceptable false acceptance rate on held-out data; track performance
by template, language, and source quality. No extraction-quality benchmark is claimed here.

Record the model version/digest and prompt version for releases. The stored provider string
includes model tag and prompt version; tags alone are mutable. Add model digests and monitoring
before relying on reproducible production inference. Monitor validation failures, review
corrections, provider errors, and drift after model/prompt changes.

## Project layout

```text
src/docureview/
  api.py          HTTP routes, authentication, request limits
  config.py       Environment configuration
  documents.py    Text and PDF parsing
  extraction.py   Demo and Ollama providers
  models.py       Typed extraction, invoice, and review schemas
  validation.py   Evidence checks and routing policy
  store.py        SQLite persistence and atomic review decisions
tests/            API and provider tests
examples/         Synthetic invoice fixture
.github/workflows/ci.yml
```

## Repository

Source: [SatyaGattu-2809/docureview](https://github.com/SatyaGattu-2809/docureview).

```bash
git clone https://github.com/SatyaGattu-2809/docureview.git
cd docureview
```

Follow the quick start above to install dependencies and run the API. Never commit credentials,
real sensitive documents, or runtime data. No license is selected; choose a license before
inviting reuse or contributions.
