import asyncio
import secrets
import sqlite3
from contextlib import asynccontextmanager, suppress
from importlib.resources import files
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import APIKeyHeader
from starlette.concurrency import run_in_threadpool

from docureview.config import Identity, Settings
from docureview.documents import DocumentError
from docureview.extraction import DemoExtractor, Extractor, OllamaExtractor, ProviderError
from docureview.models import Document, ReviewRequest
from docureview.processing import process
from docureview.store import QueueFull, ReviewConflict, Store

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
MEDIA_TYPES = {"application/pdf", "text/plain", "image/png", "image/jpeg"}


def create_app(settings: Settings | None = None, extractor: Extractor | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.database)

    async def maintenance():
        while True:
            await asyncio.sleep(60)
            try:
                await run_in_threadpool(store.purge)
            except sqlite3.OperationalError:
                pass  # Access checks still exclude expired records; retry on the next tick.

    @asynccontextmanager
    async def lifespan(app):
        store.initialize(settings.retention_days)
        with httpx.Client(timeout=httpx.Timeout(120, connect=5), trust_env=False) as client:
            app.state.extractor = extractor or (
                DemoExtractor()
                if settings.provider == "demo"
                else OllamaExtractor(client, settings.ollama_url, settings.ollama_model)
            )
            task = asyncio.create_task(maintenance())
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="DocuReview",
        version="0.2.0",
        lifespan=lifespan,
        description="Invoice extraction with source evidence and human review.",
    )

    def identity(key: Annotated[str | None, Depends(api_key_header)]) -> Identity:
        supplied = (key or "").encode()
        for principal in settings.principals():
            if secrets.compare_digest(supplied, principal.key.get_secret_value().encode()):
                return principal
        raise HTTPException(401, "Invalid API key")

    def reviewer(principal: Annotated[Identity, Depends(identity)]) -> Identity:
        if principal.role != "reviewer":
            raise HTTPException(403, "Reviewer credentials required")
        return principal

    Principal = Annotated[Identity, Depends(identity)]
    Reviewer = Annotated[Identity, Depends(reviewer)]

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path in {"/", "/review"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' blob:; frame-src blob:; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            status_code=422,
            content={
                "detail": [{"loc": error["loc"], "type": error["type"]} for error in exc.errors()]
            },
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error(request, exc):
        return JSONResponse(status_code=503, content={"detail": "Document store unavailable"})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/review", response_class=HTMLResponse, include_in_schema=False)
    def review_ui():
        return files("docureview").joinpath("static/index.html").read_text()

    @app.get("/assets/{name}", include_in_schema=False)
    def asset(name: str):
        types = {"review.js": "text/javascript", "style.css": "text/css"}
        if name not in types:
            raise HTTPException(404, "Asset not found")
        return Response(
            files("docureview").joinpath("static", name).read_text(), media_type=types[name]
        )

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/readyz", dependencies=[Depends(identity)])
    def ready():
        from contextlib import closing

        with closing(store.connect()) as connection:
            connection.execute("SELECT count(*) FROM documents").fetchone()
        # Model readiness is separate from API/database readiness.
        return {"database": "ready", "provider": settings.provider, "auto_accept": False}

    @app.get("/v1/me")
    def me(principal: Principal):
        return {"name": principal.name, "tenant": principal.tenant, "role": principal.role}

    async def read_upload(request):
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in MEDIA_TYPES:
            raise HTTPException(415, "Use application/pdf, text/plain, image/png or image/jpeg")
        if media_type.startswith("image/") and not settings.ocr_enabled:
            raise HTTPException(415, "Enable OCR to upload images")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > settings.max_upload_bytes:
                raise HTTPException(413, "Upload exceeds the size limit")
            data.extend(chunk)
        if not data:
            raise HTTPException(422, "Document is empty")
        return bytes(data), media_type

    @app.post(
        "/v1/documents",
        response_model=Document,
        status_code=201,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    media: {"schema": {"type": "string", "format": "binary"}}
                    for media in MEDIA_TYPES
                },
            }
        },
    )
    async def upload(request: Request, principal: Principal):
        """Synchronous compatibility route. Use /v1/jobs for durable processing."""
        data, media_type = await read_upload(request)

        def execute():
            try:
                document = process(
                    data,
                    media_type,
                    settings,
                    app.state.extractor,
                    principal.tenant,
                    principal.name,
                )
                store.insert(document, data)
                return document
            except DocumentError as exc:
                raise HTTPException(422, str(exc)) from exc
            except ProviderError as exc:
                raise HTTPException(502, str(exc)) from exc

        return await run_in_threadpool(execute)

    @app.post("/v1/jobs", status_code=202)
    async def enqueue(
        request: Request,
        principal: Principal,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
    ):
        data, media_type = await read_upload(request)
        try:
            return await run_in_threadpool(
                store.enqueue,
                data,
                media_type,
                principal,
                idempotency_key,
                settings.retention_days,
                settings.max_pending_jobs,
            )
        except ReviewConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except QueueFull as exc:
            raise HTTPException(503, str(exc), headers={"Retry-After": "10"}) from exc

    @app.get("/v1/jobs/{job_id}")
    def job_status(job_id: str, principal: Principal):
        job = store.job(job_id, principal)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    @app.delete("/v1/jobs/{job_id}", status_code=204)
    def cancel_job(job_id: str, principal: Reviewer):
        if not store.delete_job(job_id, principal.tenant):
            raise HTTPException(
                404, "Pending job not found; delete completed documents by document ID"
            )
        return Response(status_code=204)

    @app.get("/v1/documents/{document_id}", response_model=Document)
    def get_document(document_id: str, principal: Principal):
        document = store.get(document_id, principal)
        if document is None:
            raise HTTPException(404, "Document not found")
        return document

    @app.get("/v1/documents/{document_id}/original")
    def original(document_id: str, principal: Principal):
        document = store.get(document_id, principal)
        payload = store.original(document_id, principal)
        if document is None or payload is None:
            raise HTTPException(404, "Original is unavailable or expired")
        return Response(
            payload,
            media_type=document.media_type,
            headers={
                "Content-Disposition": 'attachment; filename="original"',
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    @app.delete("/v1/documents/{document_id}", status_code=204)
    def delete_document(document_id: str, principal: Reviewer):
        if not store.delete(document_id, principal.tenant):
            raise HTTPException(404, "Document not found")
        return Response(status_code=204)

    @app.get("/v1/reviews", response_model=list[Document])
    def review_queue(
        principal: Reviewer, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)
    ):
        return store.pending(limit, offset, principal.tenant)

    @app.post("/v1/documents/{document_id}/review", response_model=Document)
    def review_document(document_id: str, review: ReviewRequest, principal: Reviewer):
        try:
            return store.review(document_id, review, principal.name, principal.tenant)
        except KeyError as exc:
            raise HTTPException(404, "Document not found") from exc
        except ReviewConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return app
