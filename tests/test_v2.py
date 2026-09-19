import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from docureview.api import create_app
from docureview.config import Identity, Settings
from docureview.extraction import DemoExtractor, ProviderError
from docureview.models import CandidateField, Invoice, SourcePage
from docureview.normalization import normalize
from docureview.processing import process
from docureview.store import Store
from docureview.validation import assess, locate
from docureview.worker import run_once

SAMPLE = (Path(__file__).parents[1] / "examples/invoice.txt").read_text()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database=tmp_path / "store.db",
        identities=[
            Identity(name="alice", tenant="a", role="uploader", key="alice-upload-key-123"),
            Identity(name="bob", tenant="a", role="uploader", key="bob-upload-key-12345"),
            Identity(name="rhea", tenant="a", role="reviewer", key="rhea-review-key-1234"),
            Identity(name="ravi", tenant="b", role="reviewer", key="ravi-review-key-1234"),
        ],
    )


def headers(settings, name, media="text/plain"):
    principal = next(p for p in settings.identities if p.name == name)
    return {"X-API-Key": principal.key.get_secret_value(), "Content-Type": media}


def post(client, settings, text=SAMPLE):
    response = client.post("/v1/documents", content=text, headers=headers(settings, "alice"))
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    "field,raw,currency,expected",
    [
        ("invoice_date", "September 1, 2026", None, "2026-09-01"),
        ("invoice_date", "1 September 2026", None, "2026-09-01"),
        ("invoice_date", "09/01/2026", None, None),
        ("invoice_date", "31/01/2026", None, "2026-01-31"),
        ("invoice_date", "02/30/2026", None, None),
        ("total", "USD 1,080.00", "USD", "1080.00"),
        ("total", "€ 1.080,00", "EUR", "1080.00"),
        ("total", "$ 1,080.00", "USD", "1080.00"),
        ("total", "$ 1,080.00", "EUR", None),
        ("total", "1,00,0.00", "USD", None),
        ("total", "NaN", "USD", None),
        ("currency", "$", None, None),
        ("currency", "usd", None, "USD"),
    ],
)
def test_normalization(field, raw, currency, expected):
    assert normalize(field, raw, currency) == expected


def test_substring_is_not_evidence():
    candidate = CandidateField(value="80.00", confidence=0.99, evidence="Total: 1080.00")
    assert locate(candidate, [SourcePage(page=1, text="Total: 1080.00")]) is None


def test_normalized_date_has_exact_page_span():
    text = SAMPLE.replace("2026-09-01", "September 1, 2026")
    extraction = DemoExtractor().extract(text)
    extraction.invoice_date.value = "2026-09-01"
    result = assess(extraction, [SourcePage(page=2, text=text)], 0.9, True)
    field = next(c for c in result.field_checks if c.field == "invoice_date")
    assert field.source.page == 2
    assert text[field.source.start : field.source.end] == "September 1, 2026"
    assert field.normalized_value == "2026-09-01"
    assert result.status == "auto_accepted"


def test_ambiguous_location_and_wrong_page():
    candidate = CandidateField(
        value="10.00", raw_value="10.00", confidence=1, evidence="Tax: 10.00"
    )
    pages = [SourcePage(page=i, text="Tax: 10.00") for i in [1, 2]]
    assert locate(candidate, pages) is None
    candidate.page = 2
    assert locate(candidate, pages).page == 2
    candidate.page = 3
    assert locate(candidate, pages) is None


def test_conflicting_totals_cannot_be_accepted():
    extraction = DemoExtractor().extract(SAMPLE)
    result = assess(extraction, SAMPLE + "Total: 1200.00\n", 0.9, True)
    assert result.status == "needs_review"
    assert "total: conflicting source values" in result.issues


def test_originals_identity_review_and_deletion(settings):
    with TestClient(create_app(settings)) as client:
        document = post(client, settings)
        url = f"/v1/documents/{document['id']}"
        for name in ["bob", "ravi"]:
            assert client.get(url, headers=headers(settings, name)).status_code == 404
            assert client.get(url + "/original", headers=headers(settings, name)).status_code == 404
        assert (
            client.get(url + "/original", headers=headers(settings, "alice")).content
            == SAMPLE.encode()
        )
        assert client.get(url + "/original").status_code == 401
        assert client.get("/v1/reviews", headers=headers(settings, "ravi")).json() == []
        assert client.delete(url, headers=headers(settings, "alice")).status_code == 403
        body = {"expected_version": 1, "decision": "approved"}
        assert (
            client.post(
                url + "/review", json=body, headers=headers(settings, "ravi", "application/json")
            ).status_code
            == 404
        )
        response = client.post(
            url + "/review", json=body, headers=headers(settings, "rhea", "application/json")
        )
        assert response.json()["reviews"][0]["reviewer"] == "rhea"
        assert client.delete(url, headers=headers(settings, "rhea")).status_code == 204
        assert client.get(url + "/original", headers=headers(settings, "rhea")).status_code == 404
    with closing(sqlite3.connect(settings.database)) as connection:
        assert connection.execute("SELECT count(*) FROM originals").fetchone()[0] == 0


def test_expiry_blocks_reads_and_purges_bytes(settings):
    with TestClient(create_app(settings)) as client:
        doc = post(client, settings)
        with closing(sqlite3.connect(settings.database)) as connection, connection:
            connection.execute("UPDATE documents SET expires_at='2000-01-01T00:00:00+00:00'")
        url = f"/v1/documents/{doc['id']}"
        assert client.get(url, headers=headers(settings, "alice")).status_code == 404
        assert client.get(url + "/original", headers=headers(settings, "alice")).status_code == 404
        assert client.get("/v1/reviews", headers=headers(settings, "rhea")).json() == []
        Store(settings.database).purge()
    with closing(sqlite3.connect(settings.database)) as connection:
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM originals").fetchone()[0] == 0


def enqueue(client, settings, key="invoice-1"):
    return client.post(
        "/v1/jobs", content=SAMPLE, headers={**headers(settings, "alice"), "Idempotency-Key": key}
    )


def test_durable_idempotent_jobs_and_access(settings):
    with TestClient(create_app(settings)) as client:
        first = enqueue(client, settings)
        assert first.status_code == 202
        job_id = first.json()["id"]
        assert enqueue(client, settings).json()["id"] == job_id
        response = client.post(
            "/v1/jobs",
            content=SAMPLE + "changed",
            headers={**headers(settings, "alice"), "Idempotency-Key": "invoice-1"},
        )
        assert response.status_code == 409
        assert client.get(f"/v1/jobs/{job_id}", headers=headers(settings, "bob")).status_code == 404
    # A new process/app can consume a job created by the previous app lifetime.
    store = Store(settings.database)
    assert run_once(store, settings, DemoExtractor())
    with TestClient(create_app(settings)) as client:
        job = client.get(f"/v1/jobs/{job_id}", headers=headers(settings, "alice")).json()
        assert job["state"] == "succeeded"
        assert job["attempts"] == 1
        doc = client.get(
            f"/v1/documents/{job['document_id']}", headers=headers(settings, "alice")
        ).json()
        assert doc["status"] == "needs_review"
        assert (
            client.get(
                f"/v1/documents/{doc['id']}/original", headers=headers(settings, "rhea")
            ).content
            == SAMPLE.encode()
        )
        assert enqueue(client, settings).json()["document_id"] == doc["id"]


def test_deleted_job_cannot_be_resurrected(settings):
    with TestClient(create_app(settings)) as client:
        job = enqueue(client, settings).json()
        store = Store(settings.database)
        claimed = store.claim()
        assert (
            client.delete(f"/v1/jobs/{job['id']}", headers=headers(settings, "rhea")).status_code
            == 204
        )
        document = process(SAMPLE.encode(), "text/plain", settings, DemoExtractor(), "a", "alice")
        assert store.finish(claimed, document) is False
        assert store.get(document.id) is None


def test_worker_retry_and_expired_lease_fencing(settings):
    with TestClient(create_app(settings)) as client:
        enqueue(client, settings)
    store = Store(settings.database)
    stale = store.claim()
    with closing(store.connect()) as connection, connection:
        connection.execute("UPDATE jobs SET lease_until='2000-01-01T00:00:00+00:00'")
    reclaimed = store.claim()
    assert reclaimed["attempts"] == 2
    document = process(SAMPLE.encode(), "text/plain", settings, DemoExtractor(), "a", "alice")
    assert store.finish(stale, document) is False
    assert store.finish(reclaimed, document) is True


def test_retry_limit(settings):
    class Failing:
        def extract(self, text):
            raise ProviderError("Private upstream error")

    with TestClient(create_app(settings)) as client:
        job = enqueue(client, settings).json()
        store = Store(settings.database)
        for attempt in range(3):
            assert run_once(store, settings, Failing())
            with closing(store.connect()) as connection, connection:
                connection.execute("UPDATE jobs SET available_at='2000-01-01T00:00:00+00:00'")
        result = client.get(f"/v1/jobs/{job['id']}", headers=headers(settings, "alice")).json()
        assert result["state"] == "failed"
        assert result["error"] == "Extraction provider failed"
        with closing(store.connect()) as connection:
            assert connection.execute("SELECT payload FROM jobs").fetchone()[0] is None


def test_capacity_and_secure_ui(settings):
    settings.max_pending_jobs = 1
    with TestClient(create_app(settings)) as client:
        assert enqueue(client, settings).status_code == 202
        assert enqueue(client, settings, "another-key").status_code == 503
        response = client.get("/review")
        assert response.status_code == 200
        assert "script-src 'self'" in response.headers["Content-Security-Policy"]
        assert response.headers["Cache-Control"] == "no-store"
        assert client.get("/assets/review.js").status_code == 200
        assert client.get("/assets/nope").status_code == 404
        assert client.get("/readyz").status_code == 401
        assert (
            client.get("/readyz", headers=headers(settings, "alice")).json()["auto_accept"] is False
        )


def test_auto_accept_is_locked(settings):
    values = settings.model_dump()
    with pytest.raises(ValidationError, match="locked off"):
        Settings(**{**values, "auto_accept": True})


def test_rich_invoice_arithmetic():
    base = {
        "invoice_number": "INV-1",
        "vendor": "Test",
        "invoice_date": "2026-09-01",
        "currency": "USD",
        "subtotal": "1000.00",
        "tax": "80.00",
        "shipping": "10.00",
        "discount": "20.00",
        "total": "1070.00",
        "line_items": [
            {"description": "Service", "quantity": "2", "unit_price": "500.00", "amount": "1000.00"}
        ],
    }
    assert Invoice.model_validate(base).total == 1070
    for change in [
        {"total": "1080.00"},
        {"subtotal": "999.00"},
        {
            "line_items": [
                {
                    "description": "Service",
                    "quantity": "3",
                    "unit_price": "500.00",
                    "amount": "1000.00",
                }
            ]
        },
    ]:
        with pytest.raises(ValidationError):
            Invoice.model_validate({**base, **change})


def test_ocr_always_requires_review():
    extraction = DemoExtractor().extract(SAMPLE)
    result = assess(extraction, [SourcePage(page=1, text=SAMPLE, method="ocr")], 0.9, True)
    assert result.status == "needs_review"


def test_legacy_migration(settings):
    old = process(
        SAMPLE.encode(), "text/plain", settings, DemoExtractor(), "local", "local-uploader"
    )
    legacy = old.model_dump(mode="json")
    for key in ["tenant", "owner", "expires_at", "source_pages", "media_type", "ocr_used"]:
        legacy.pop(key)
    with closing(sqlite3.connect(settings.database)) as connection, connection:
        connection.execute(
            "CREATE TABLE documents (id TEXT PRIMARY KEY,status TEXT,created_at TEXT,body TEXT)"
        )
        connection.execute(
            "INSERT INTO documents VALUES (?,?,?,?)",
            (old.id, old.status, old.created_at.isoformat(), json.dumps(legacy)),
        )
    store = Store(settings.database)
    store.initialize()
    migrated = store.get(old.id)
    assert migrated.expires_at == old.created_at + timedelta(days=7)
    assert migrated.tenant == "local"


def test_expired_job_cannot_finish(settings):
    with TestClient(create_app(settings)) as client:
        enqueue(client, settings)
    store = Store(settings.database)
    job = store.claim()
    with closing(store.connect()) as connection, connection:
        connection.execute(
            "UPDATE jobs SET expires_at=?", ((datetime.now(UTC) - timedelta(days=1)).isoformat(),)
        )
    document = process(SAMPLE.encode(), "text/plain", settings, DemoExtractor(), "a", "alice")
    assert store.finish(job, document) is False
