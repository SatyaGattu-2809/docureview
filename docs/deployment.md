# Deployment boundaries and decisions

## Current supported topology

One Linux host, one API process, one worker process, one SQLite database on local persistent
storage. Docker Compose supplies both processes, a shared volume, non-root containers,
read-only root filesystems, memory/CPU limits, and bounded temporary storage. Do not mount
the same SQLite database on multiple hosts or a network filesystem for horizontal scaling.

SQLite remains appropriate for this modest single-host portfolio deployment. PostgreSQL
was not added without a demonstrated topology/concurrency need. Migrate before multiple
hosts, independently scaling workers, high write contention, or availability requirements.
At that point use versioned migrations, normalized tenant/job/review tables, transactional
job claims (e.g. row locking), durable object storage, and an explicit backup/restore plan.

## Identity and access

Legacy local mode accepts one uploader key and one reviewer key. Shared mode uses a JSON
file of individual credentials. Unset both legacy key environment variables, then set
`DOCUREVIEW_IDENTITIES_FILE` to an operator-provisioned file outside the repository:

```json
[
  {"name":"alice","tenant":"finance-a","role":"uploader","key":"REPLACE_WITH_GENERATED_SECRET_1"},
  {"name":"rhea","tenant":"finance-a","role":"reviewer","key":"REPLACE_WITH_GENERATED_SECRET_2"}
]
```

The uppercase key strings are placeholders. Generate distinct random secrets of at least
32 random bytes and restrict the file to the service account. Duplicate keys/identity names
are rejected at startup. Never put actual keys into Git. Uploaders read their own documents
and jobs; reviewers can review/read/delete within their tenant. All original-file access
uses the same checks. A reviewer identity is recorded with each decision. Other tenants
receive 404. API keys are kept only in browser-tab memory, not cookies, URLs or web storage.
The UI uses text nodes for document content and a restrictive content security policy.

These are named service credentials, not SSO, MFA, or short-lived user sessions. Rotate keys
by replacing the file and restarting services. Before broad organizational use, add a
trusted OIDC provider, verified issuer/audience/expiry, centralized role mapping, and session
revocation. Do not accept an unverified username/tenant header from clients.

## Durable jobs

`POST /v1/jobs` stores bytes and returns 202 with a job ID. `Idempotency-Key` is required and
scoped to tenant and uploader. Reusing it with identical content returns the same job;
different content/media type returns 409. The global pending/running cap defaults to 100.
The synchronous `/v1/documents` endpoint remains for compatibility and has no idempotency.
Use ingress limits to prevent synchronous calls from overwhelming parser/model resources.

The worker claims a transactionally leased job. States are queued, running, succeeded and
failed. A crashed worker's 10-minute lease can be reclaimed. Attempts are capped at three;
provider failures use exponential retry delays. Invalid documents fail immediately. A
lease token fences stale worker results, and completion plus document/original insertion
is one transaction. A deleted or expired job cannot be resurrected by in-flight completion.
Processing can happen more than once after a crash; result persistence is fenced, not
exactly-once inference. Timeout configuration is not a distributed cancellation guarantee.

Failed jobs retain status but discard uploaded bytes. Reviewer DELETE of a pending job
removes it; work already in progress may finish computing but cannot persist a result.
Completed jobs must be removed through document deletion. Retrying after retention expiry
or deletion can create a new job because the idempotency record is also removed.

## Originals, expiry and deletion

Original bytes, page text, extracted values, corrections and review notes are stored in the
local database. Default retention is seven days from upload; configurable 1–30 days. Worker
completion inherits the job's expiry, so queue delays never extend retention. Access checks
exclude expired records even before cleanup. A maintenance task purges every 60 seconds;
worker startup/iterations also purge. An offline service cannot physically purge until it
runs again. Schedule `python -m docureview.worker --purge-only` if needed while the API is off.

Reviewer document deletion removes its original, extraction, review history and associated
job. SQLite secure-delete is enabled, but this is **not a forensic-erasure guarantee** for
WAL files, filesystem snapshots, SSDs, or backups. Use encrypted storage, restrictive volume
permissions, and a separate backup-retention policy. Downloads and browser memory already
held by a user cannot be remotely revoked. Migration of old records assigns expiry from
original creation time; old originals were never retained and cannot be recovered.

## Parsing and OCR

Parsing runs in a child process with a wall timeout (60 seconds by default), not in the API
process. On Linux it has a 1 GiB address-space limit, a 45-second CPU limit and a 64 MiB file
size limit. The parent kills the process group on timeout. PDF pages without text can use
optional English Tesseract OCR; rasterization is capped at 2400 pixels on the long edge.
OCR always forces review. The parser does not preserve table layout or bounding boxes;
evidence spans refer to extracted page text, including OCR text, not PDF byte offsets.

Linux containers are the hardened target. Windows lacks these POSIX resource/process-group
limits, although subprocess timeouts still apply. The worker shares the application's
credentials/environment and can launch native parsers; process isolation is not a sandbox
against native-code exploits. Use network-disabled, separately credentialed worker isolation
before accepting arbitrary public uploads. Add ingress body/time/rate limits, malware policy
where required, and per-tenant quotas before shared public deployment.

## Readiness and remaining operations

`/healthz` is public process liveness. Authenticated `/readyz` checks database access and
reports the configured provider; it does not prove that a model is loaded or a worker is
healthy. Add model readiness, worker heartbeat, queue-age/error metrics, alerting, centralized
redacted logs and recovery drills when running a service for others. The current SQLite
audit record is attributable but not append-only or tamper-proof. Use TLS for any remote
access and keep the default localhost binding until an authenticated gateway is configured.
