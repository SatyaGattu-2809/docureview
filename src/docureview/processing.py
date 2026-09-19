import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from docureview.config import Settings
from docureview.documents import read_pages
from docureview.models import Document
from docureview.validation import assess


def process(data, media_type, settings: Settings, extractor, tenant, owner, expires_at=None):
    pages = read_pages(data, media_type, settings)
    # Page markers are context, not evidence: spans are resolved against original page text.
    text = "\n".join(f"[PAGE {page.page}]\n{page.text}" for page in pages)
    extraction = extractor.extract(text)
    assessment = assess(extraction, pages, settings.review_threshold, False)
    return Document(
        id=str(uuid4()),
        sha256=hashlib.sha256(data).hexdigest(),
        created_at=datetime.now(UTC),
        provider=extractor.name,
        tenant=tenant,
        owner=owner,
        expires_at=expires_at or datetime.now(UTC) + timedelta(days=settings.retention_days),
        source_pages=pages,
        media_type=media_type,
        ocr_used=any(p.method == "ocr" for p in pages),
        status="needs_review",
        extraction=extraction,
        assessment=assessment,
        final_invoice=None,
    )
