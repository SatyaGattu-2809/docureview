# Release acceptance record

This change improves the single-host, human-reviewed application. It does not certify an
unconfigured deployment as production ready. Automatic acceptance remains disabled.

## Implemented scope

- Header extraction and source evidence; explicit conservative normalization.
- Browser correction, approval, rejection, original access and deletion.
- Durable idempotent jobs with leases, retries and completion fencing.
- Named tenant credentials, access checks, expiry and cleanup.
- Separate header, richer invoice and OCR evaluation tracks.
- Optional worker readiness, durable-only API mode, tenant queue diagnostics.
- Consistent protected database snapshots and an offline restore procedure.
- Minimal parser subprocess environment (not a native-code security sandbox).

## Extraction evidence

`DOCUREVIEW_PROVIDER=labels` selects a deterministic parser for colon, pipe, equals and
multiline labeled text. `demo` keeps the original baseline unchanged. Neither is an LLM.
Run `python evaluation/run.py --provider labels --output /tmp/labels.json`.
All 24 existing cases have been inspected during development. Split names in historical
reports are retained for comparison only. These cases are now regression data, and any
perfect score is not evidence of production accuracy or generalization.

For a real model, configure an operator-controlled `OLLAMA_URL` and `OLLAMA_MODEL`, then run:

```
python evaluation/run.py --provider ollama --dataset /secure/unseen-invoices.json --output /secure/model-report.json
```

The dataset uses the existing schema and must contain disjoint vendor/template groups.
Keep new test vendors unseen during tuning, use permissioned representative data, and
report per-layout present-field accuracy, full-invoice accuracy, provider failures and
latency. Record the model digest, runtime/hardware and prompt version with the report.
Evaluate OCR and line items separately. Do not present mocked transport tests as inference.
Choose acceptance thresholds with the deployment owner before measuring the test set.
Human review is required even when the diagnostic shadow policy would accept a document.

## Gates requiring a target environment

| Gate | Evidence required before release |
| --- | --- |
| Actual model quality | Reproducible real inference against representative unseen invoices |
| Exposure and identity | Target host/domain, TLS, ingress body/rate/time limits; named identities or organizational SSO |
| Data protection | Encrypted persistent storage, backup retention and deletion policy |
| Parser isolation | Restricted filesystem/credentials and network policy for untrusted native parsers |
| Capacity | Agreed traffic target; queue-age, latency, contention and failure measurements |
| Recovery | Restore drill on the actual volume; deletion reconciliation and restart smoke test |
| Operations | Alert routing, dependency/image vulnerability review, named owner and rollback procedure |

SQLite is supported only on a single host with local storage. Multi-host/HA requirements
need a transactional PostgreSQL job store and durable object storage before deployment.
Do not describe this scope as multi-host production readiness.

## Verification for this change

- Python suite: 109 passed, including live Tesseract parsing, role/tenant access,
  durable-job fencing, new observed-layout regression cases and snapshot recovery.
- Lint and formatting: passed.
- Original demo baseline: unchanged regression checks passed.
- `labels:v2`: 24/24 expected header records matched on the inspected synthetic set.
- Rich track: four synthetic cases passed expected validation outcomes.
- OCR track: two synthetic English scans had zero character error and matching headers
  with Tesseract 5.3.4. This tiny set does not establish OCR robustness.
- Local browser verification blocked by a failed Chromium archive download.
- Local container verification unavailable (Docker is not installed).
- Live model not measured: no model runtime, endpoint or model tag configured here.
- CI browser/container results must be inspected on the pull request before merging.
