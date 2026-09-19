from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from docureview.api import create_app
from docureview.config import Settings
from docureview.extraction import DemoExtractor, OllamaExtractor, ProviderError
from docureview.models import ReviewRequest
from docureview.store import ReviewConflict, Store
from docureview.validation import assess

SAMPLE = (Path(__file__).parents[1] / "examples/invoice.txt").read_text()
UPLOAD_KEY = "uploader-test-key-12345"
REVIEW_KEY = "reviewer-test-key-12345"
UPLOAD_HEADERS = {"X-API-Key": UPLOAD_KEY, "Content-Type": "text/plain"}
REVIEW_HEADERS = {"X-API-Key": REVIEW_KEY}


@pytest.fixture
def settings(tmp_path):
    return Settings(api_key=UPLOAD_KEY, reviewer_key=REVIEW_KEY, database=tmp_path / "documents.db")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


def upload(client, text=SAMPLE):
    response = client.post("/v1/documents", content=text, headers=UPLOAD_HEADERS)
    assert response.status_code == 201, response.text
    return response.json()


def test_upload_review_and_conflict(client):
    document = upload(client)
    assert document["status"] == "needs_review"
    assert document["assessment"]["invoice"]["total"] == "1080.00"
    assert document["final_invoice"] is None
    queue = client.get("/v1/reviews", headers=REVIEW_HEADERS).json()
    assert [item["id"] for item in queue] == [document["id"]]
    url = f"/v1/documents/{document['id']}/review"
    body = {"expected_version": 1, "decision": "approved", "note": "Verified against source"}
    response = client.post(url, json=body, headers=REVIEW_HEADERS)
    assert response.status_code == 200
    reviewed = response.json()
    assert reviewed["version"] == 2
    assert reviewed["final_invoice"]["total"] == "1080.00"
    assert reviewed["reviews"][0]["reviewer"] == "local-reviewer"
    assert client.post(url, json=body, headers=REVIEW_HEADERS).status_code == 409
    assert client.get("/v1/reviews", headers=REVIEW_HEADERS).json() == []


def test_bad_totals_require_correction(client):
    document = upload(client, SAMPLE.replace("1080.00", "999.00"))
    assert document["assessment"]["invoice"] is None
    url = f"/v1/documents/{document['id']}/review"
    body = {"expected_version": 1, "decision": "approved"}
    assert client.post(url, json=body, headers=REVIEW_HEADERS).status_code == 422
    corrected = assess(DemoExtractor().extract(SAMPLE), SAMPLE, 0.9, False).invoice
    body["corrected_invoice"] = corrected.model_dump(mode="json")
    result = client.post(url, json=body, headers=REVIEW_HEADERS)
    assert result.status_code == 200
    assert result.json()["extraction"]["total"]["value"] == "999.00"
    assert result.json()["final_invoice"]["total"] == "1080.00"


def test_auth_roles_and_not_found(client):
    assert client.post("/v1/documents", content=SAMPLE).status_code == 401
    assert client.get("/v1/reviews", headers=UPLOAD_HEADERS).status_code == 403
    assert client.get("/v1/documents/missing", headers=UPLOAD_HEADERS).status_code == 404
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize(
    "body,media_type,expected",
    [
        (b"", "text/plain", 422),
        (b"\xff", "text/plain", 422),
        (b"abc", "application/pdf", 422),
        (b"%PDF-broken", "application/pdf", 422),
        (b"\x00abc", "text/plain", 422),
        (b"abc", "image/png", 415),
    ],
)
def test_invalid_uploads(client, body, media_type, expected):
    headers = {**UPLOAD_HEADERS, "Content-Type": media_type}
    assert client.post("/v1/documents", content=body, headers=headers).status_code == expected


def test_upload_and_text_limits(settings):
    settings.max_upload_bytes = 20
    with TestClient(create_app(settings)) as client:
        assert (
            client.post("/v1/documents", content="x" * 21, headers=UPLOAD_HEADERS).status_code
            == 413
        )
    settings.max_upload_bytes = 100
    settings.max_text_chars = 10
    with TestClient(create_app(settings)) as client:
        assert (
            client.post("/v1/documents", content="x" * 11, headers=UPLOAD_HEADERS).status_code
            == 422
        )


def test_blank_and_encrypted_pdf(client):
    for encrypted in (False, True):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        if encrypted:
            writer.encrypt("test-password")
        buffer = BytesIO()
        writer.write(buffer)
        response = client.post(
            "/v1/documents",
            content=buffer.getvalue(),
            headers={**UPLOAD_HEADERS, "Content-Type": "application/pdf"},
        )
        assert response.status_code == 422
        assert ("Encrypted" if encrypted else "OCR") in response.text


def test_persistence(settings):
    with TestClient(create_app(settings)) as client:
        document = upload(client)
    with TestClient(create_app(settings)) as client:
        result = client.get(f"/v1/documents/{document['id']}", headers=UPLOAD_HEADERS)
        assert result.json()["sha256"] == document["sha256"]


def test_evidence_and_threshold():
    extraction = DemoExtractor().extract(SAMPLE)
    assert assess(extraction, SAMPLE, 0.9, True).status == "auto_accepted"
    assert assess(extraction, SAMPLE, 0.99, True).status == "needs_review"
    extraction.total.evidence = "Total: 9999.00"
    result = assess(extraction, SAMPLE, 0.9, True)
    assert result.status == "needs_review"
    assert result.confidence == 0


def test_missing_and_duplicate_fields(client):
    document = upload(client, SAMPLE + "Total: 1081.00\n")
    assert document["extraction"]["total"]["value"] is None
    document = upload(client, "Vendor: Only vendor provided\n")
    assert document["status"] == "needs_review"
    assert document["assessment"]["invoice"] is None


def test_demo_never_auto_accepts(settings):
    settings.auto_accept = True
    with TestClient(create_app(settings)) as client:
        assert upload(client)["status"] == "needs_review"


def test_provider_success():
    def respond(request):
        import json

        body = json.loads(request.content)
        assert body["stream"] is False
        assert "properties" in body["format"]
        return httpx.Response(
            200,
            json={
                "done": True,
                "message": {"content": DemoExtractor().extract(SAMPLE).model_dump_json()},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        extractor = OllamaExtractor(client, "http://localhost:11434", "test-model")
        assert extractor.extract(SAMPLE).total.value == "1080.00"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"done": False},
        {"done": True, "message": {"content": "not json"}},
        {"done": True, "done_reason": "length"},
    ],
)
def test_provider_invalid_response(payload):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(ProviderError):
            OllamaExtractor(client, "http://localhost:11434", "test-model").extract(SAMPLE)


def test_provider_failure_returns_502_without_storage(settings):
    class FailingExtractor:
        name = "failing"

        def extract(self, text):
            raise ProviderError("Extraction provider unavailable")

    with TestClient(create_app(settings, FailingExtractor())) as client:
        response = client.post("/v1/documents", content=SAMPLE, headers=UPLOAD_HEADERS)
        assert response.status_code == 502
        assert client.get("/v1/reviews", headers=REVIEW_HEADERS).json() == []


def test_concurrent_reviews_have_one_winner(client, settings):
    document = upload(client)
    store = Store(settings.database)
    request = ReviewRequest(expected_version=1, decision="approved")

    def review():
        try:
            store.review(document["id"], request, "test-reviewer")
            return "approved"
        except ReviewConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: review(), range(2)))
    assert sorted(results) == ["approved", "conflict"]


def test_rejection_and_invalid_correction(client):
    document = upload(client)
    url = f"/v1/documents/{document['id']}/review"
    body = {
        "expected_version": 1,
        "decision": "approved",
        "corrected_invoice": {**document["assessment"]["invoice"], "total": "10.00"},
    }
    assert client.post(url, json=body, headers=REVIEW_HEADERS).status_code == 422
    result = client.post(
        url, json={"expected_version": 1, "decision": "rejected"}, headers=REVIEW_HEADERS
    )
    assert result.json()["status"] == "rejected"
    assert result.json()["final_invoice"] is None


def test_text_pdf_upload(client):
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
    )

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    commands = ["BT /F1 12 Tf 50 750 Td 16 TL"]
    for line in SAMPLE.splitlines():
        commands.append(f"({line}) Tj T*")
    commands.append("ET")
    stream.set_data("\n".join(commands).encode())
    page[NameObject("/Contents")] = stream
    buffer = BytesIO()
    writer.write(buffer)
    response = client.post(
        "/v1/documents",
        content=buffer.getvalue(),
        headers={**UPLOAD_HEADERS, "Content-Type": "application/pdf"},
    )
    assert response.status_code == 201
    assert response.json()["assessment"]["invoice"]["total"] == "1080.00"


@pytest.mark.parametrize(
    "change", [("2026-09-01", "2026-02-30"), ("USD", "ZZZ"), ("80.00", "80.001")]
)
def test_invalid_invoice_fields(client, change):
    document = upload(client, SAMPLE.replace(*change))
    assert document["assessment"]["invoice"] is None
    assert document["status"] == "needs_review"


def test_provider_timeout():
    def timeout(request):
        raise httpx.ReadTimeout("Upstream response body should never be exposed")

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(ProviderError, match="Extraction provider failed"):
            OllamaExtractor(client, "http://localhost:11434", "test-model").extract(SAMPLE)
