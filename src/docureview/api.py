import hashlib
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from starlette.concurrency import run_in_threadpool

from docureview.config import Settings
from docureview.documents import DocumentError, read_document
from docureview.extraction import DemoExtractor, Extractor, OllamaExtractor, ProviderError
from docureview.models import Document, ReviewRequest
from docureview.store import ReviewConflict, Store
from docureview.validation import assess

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def create_app(settings: Settings | None = None, extractor: Extractor | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.database)

    @asynccontextmanager
    async def lifespan(app):
        store.initialize()
        with httpx.Client(timeout=httpx.Timeout(120, connect=5), trust_env=False) as client:
            app.state.extractor = extractor or (
                DemoExtractor()
                if settings.provider == "demo"
                else OllamaExtractor(client, settings.ollama_url, settings.ollama_model)
            )
            yield

    app = FastAPI(
        title="DocuReview",
        version="0.1.0",
        lifespan=lifespan,
        description="Invoice extraction with evidence checks and human review.",
    )

    def role(key: Annotated[str | None, Depends(api_key_header)]) -> str:
        supplied = (key or "").encode()
        if secrets.compare_digest(supplied, settings.reviewer_key.get_secret_value().encode()):
            return "reviewer"
        if secrets.compare_digest(supplied, settings.api_key.get_secret_value().encode()):
            return "uploader"
        raise HTTPException(401, "Invalid API key")

    def reviewer(current_role: Annotated[str, Depends(role)]):
        if current_role != "reviewer":
            raise HTTPException(403, "Reviewer credentials required")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        errors = [{"loc": error["loc"], "type": error["type"]} for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error(request, exc):
        return JSONResponse(status_code=503, content={"detail": "Document store unavailable"})

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.post(
        "/v1/documents",
        response_model=Document,
        status_code=201,
        dependencies=[Depends(role)],
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/pdf": {"schema": {"type": "string", "format": "binary"}},
                    "text/plain": {"schema": {"type": "string", "format": "binary"}},
                },
            }
        },
    )
    async def upload(request: Request):
        """Send document bytes directly in the body (not multipart form data)."""
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in {"application/pdf", "text/plain"}:
            raise HTTPException(415, "Use application/pdf or text/plain")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > settings.max_upload_bytes:
                raise HTTPException(413, "Upload exceeds the size limit")
            data.extend(chunk)

        def process():
            try:
                text = read_document(bytes(data), media_type, settings)
            except DocumentError as exc:
                raise HTTPException(422, str(exc)) from exc
            try:
                extraction = app.state.extractor.extract(text)
            except ProviderError as exc:
                raise HTTPException(502, str(exc)) from exc
            assessment = assess(
                extraction,
                text,
                settings.review_threshold,
                settings.auto_accept and settings.provider != "demo",
            )
            document = Document(
                id=str(uuid4()),
                sha256=hashlib.sha256(data).hexdigest(),
                created_at=datetime.now(UTC),
                provider=app.state.extractor.name,
                status=assessment.status,
                extraction=extraction,
                assessment=assessment,
                final_invoice=assessment.invoice if assessment.status == "auto_accepted" else None,
            )
            store.insert(document)
            return document

        return await run_in_threadpool(process)

    @app.get("/v1/documents/{document_id}", response_model=Document, dependencies=[Depends(role)])
    def get_document(document_id: str):
        document = store.get(document_id)
        if document is None:
            raise HTTPException(404, "Document not found")
        return document

    @app.get("/v1/reviews", response_model=list[Document], dependencies=[Depends(reviewer)])
    def review_queue(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        return store.pending(limit, offset)

    @app.post(
        "/v1/documents/{document_id}/review",
        response_model=Document,
        dependencies=[Depends(reviewer)],
    )
    def review_document(document_id: str, review: ReviewRequest):
        try:
            return store.review(document_id, review, settings.reviewer_name)
        except KeyError as exc:
            raise HTTPException(404, "Document not found") from exc
        except ReviewConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return app
