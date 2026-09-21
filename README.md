# DocuReview

Invoice extraction with source-linked evidence, a browser review workflow, and reproducible
evaluation. Python · FastAPI · Pydantic · SQLite · optional Ollama and Tesseract.

**v0.2 is a single-host MVP. All results require human review.** Demo mode is a deterministic
parser, not an AI model. Live-model accuracy has not been measured. The evaluation report
makes that distinction explicit.

## What's included

- 24 labeled synthetic header cases with whole-vendor/template holdouts and recorded baseline.
- Per-field normalized values with exact page/character source spans; conservative date,
  amount and currency rules. `80.00` does not match inside `1080.00`.
- Review UI with original access, highlighted source text, corrections, line items, approval,
  rejection and deletion; optimistic review version checks prevent silent overwrites.
- Optional English OCR for PNG/JPEG and PDF pages without text. OCR always requires review.
- Shipping, discount and line-item arithmetic, evaluated separately from header extraction.
- Durable jobs, idempotency, leases, bounded retries, scoped identities and retention cleanup.
- API tests, real OCR smoke tests, a Playwright browser workflow and GitHub CI.

Read the [evaluation report](docs/evaluation.md) for actual metrics and limitations, and
[deployment decisions](docs/deployment.md) for identity, retention and scaling boundaries.

## Run locally

Requires Python 3.12+. Clone and install:

```bash
git clone https://github.com/SatyaGattu-2809/docureview.git
cd docureview
# While this change is under review, switch to feature/evaluation-review.
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
export DOCUREVIEW_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export DOCUREVIEW_REVIEWER_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export DOCUREVIEW_PROVIDER=demo
uvicorn docureview.api:create_app --factory --host 127.0.0.1 --port 8000 --limit-concurrency 8
```

In a second terminal, activate the same environment, set the **same configuration/keys**, and run:

```bash
python -m docureview.worker
```

Open `http://127.0.0.1:8000/review`, enter your reviewer key, and upload
`examples/invoice.txt`. Select evidence buttons to highlight values on the source page.
Use **Open original** to inspect/download the uploaded file before approving or correcting it.
Originals and extracted records expire after seven days by default. The UI retains the key
only in memory and clears it on Disconnect. `/docs` provides API documentation.

On Windows PowerShell, create/activate the environment with:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
$env:DOCUREVIEW_API_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:DOCUREVIEW_REVIEWER_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:DOCUREVIEW_PROVIDER = "demo"
```

Then use the same pip, uvicorn and worker commands above. The Linux container is the target
for parser resource isolation; Windows resource limits differ. Python does not auto-load
`.env`; set environment variables explicitly. Docker Compose reads `.env`.

## AI and OCR

Configure an already installed Ollama model that supports structured JSON, then restart both
processes:

```bash
export DOCUREVIEW_PROVIDER=ollama
export OLLAMA_URL=http://127.0.0.1:11434
export OLLAMA_MODEL='YOUR_INSTALLED_MODEL_TAG'
```

The model tag above is a placeholder. No model weights are bundled. The full extracted text
is sent to that endpoint. The [Ollama chat API](https://docs.ollama.com/api/chat) JSON-schema
format constrains output shape; local validation still checks evidence and arithmetic.
Documents are untrusted data and the model has no tool access. These controls cannot prove
that all extracted values are semantically correct.

For OCR, install Tesseract with English language data and Poppler (`pdftoppm`) on the host,
or use the supplied image. Set `DOCUREVIEW_OCR_ENABLED=true`. PDF fallback applies to pages
with no readable text; it does not detect every image containing additional information on
a page that already has some text. No handwriting or multilingual quality claim is made.
See [Tesseract's CLI documentation](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html).

## API workflow

Upload raw bytes, not multipart form data:

```bash
curl -sS http://127.0.0.1:8000/v1/jobs \
  -H "X-API-Key: $DOCUREVIEW_API_KEY" \
  -H 'Content-Type: text/plain' -H 'Idempotency-Key: invoice-demo-1' \
  --data-binary @examples/invoice.txt
```

Poll `GET /v1/jobs/{id}` using the same credentials; a successful job contains `document_id`.
For an immediate synchronous result, the compatibility route `POST /v1/documents` remains.

| Route | Role | Purpose |
| --- | --- | --- |
| `GET /v1/me` | Either | Current credential identity |
| `POST /v1/jobs` | Either | Durable upload; requires `Idempotency-Key` |
| `GET /v1/jobs/{id}` | Owner or tenant reviewer | Processing status |
| `DELETE /v1/jobs/{id}` | Tenant reviewer | Remove pending/failed job |
| `GET /v1/documents/{id}` | Owner or tenant reviewer | Fields, evidence and history |
| `GET /v1/documents/{id}/original` | Owner or tenant reviewer | Authenticated original bytes |
| `GET /v1/reviews` | Tenant reviewer | Pending queue, limit/offset pagination |
| `POST /v1/documents/{id}/review` | Tenant reviewer | Approve/correct/reject with expected version |
| `DELETE /v1/documents/{id}` | Tenant reviewer | Remove original, record and job |

Example approval payload:

```json
{"expected_version":1,"decision":"approved","note":"Verified against original"}
```

Include `corrected_invoice` when editing values. It must contain the seven required header
fields; optional `shipping`, `discount` and `line_items` default to zero/empty. A corrected
invoice is required if the original fails schema validation. Corrections are human assertions;
original extraction remains unchanged. The UI sends a complete corrected invoice.

## Rich invoice scope

Seven required headers: invoice number, vendor, invoice date, currency, subtotal, tax and total.
Optional line items have description, quantity, unit price and amount. Validation requires:

- Line amount equals quantity × unit price, rounded half-up to two decimal places.
- Supplied line amounts sum to subtotal.
- Subtotal + tax + shipping − discount equals total.

Supported currencies remain USD, EUR, GBP, INR, CAD and AUD. No negative credit notes,
multiple currencies per invoice, per-line tax/discount accounting, multiple invoices per
file, or currency-specific non-two-decimal precision. The demo parser only supports the
bundled labeled format; richer examples live in `evaluation/rich.json`.

## Configuration

| Variable | Default / behavior |
| --- | --- |
| `DOCUREVIEW_API_KEY`, `DOCUREVIEW_REVIEWER_KEY` | Distinct generated credentials required in local mode |
| `DOCUREVIEW_IDENTITIES_FILE` | Optional JSON file of named tenant credentials; replaces legacy keys |
| `DOCUREVIEW_DATABASE` | `data/docureview.sqlite3` |
| `DOCUREVIEW_PROVIDER` | `demo`; optional `ollama` |
| `DOCUREVIEW_OCR_ENABLED` | `false` |
| `DOCUREVIEW_RETENTION_DAYS` | `7`, allowed range 1–30 |
| `DOCUREVIEW_REVIEW_THRESHOLD` | `0.90`, diagnostic signal only |
| `DOCUREVIEW_AUTO_ACCEPT` | Must remain `false`; enabling it fails startup |

Upload/page/text limits remain 5 MiB / 10 pages / 12,000 characters. Review scores are
uncalibrated signals, not probabilities. Source spans reference extracted page text; OCR
spans do not prove the pixels were read correctly. Repeated indistinguishable quotes are
left unresolved for a reviewer. Completed review decisions cannot be reopened in this version.

## Checks and evaluation

```bash
ruff check .
ruff format --check .
pytest -q
python evaluation/run.py --provider demo --output /tmp/header-report.json
python evaluation/run_rich.py --output /tmp/rich-report.json
python evaluation/run_ocr.py --output /tmp/ocr-report.json
```

For the browser workflow (Node.js required):

```bash
npm ci
npx playwright install chromium
npm run test:browser
```

It starts disposable API and worker processes with synthetic data and a temporary database.
Set `PYTHON` if the intended Python interpreter is not on PATH. Model transport tests use
mock responses; live-model evaluation is a separate command described in the report.

## Containers

```bash
cp .env.example .env
# Set two distinct generated keys in .env.
docker compose up --build
```

The image includes OCR binaries; OCR remains off unless enabled. Persistent data uses a
named volume. Inside containers, host Ollama is not `127.0.0.1`: use a trusted reachable
endpoint. Do not expose either service publicly without TLS, authentication and ingress
rate/body/time limits. The included topology is one host, not a horizontally scalable service.
Named credentials provide scoped access but not enterprise SSO. Detailed retention, backup,
parser-isolation and migration limitations are in [deployment.md](docs/deployment.md).

Original files are now retained to support review; do not use sensitive real invoices until
your storage/access/retention policies are configured. No license has been selected.
