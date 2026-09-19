from io import BytesIO

from pypdf import PdfReader

from docureview.config import Settings


class DocumentError(ValueError):
    pass


def read_document(data: bytes, media_type: str, settings: Settings) -> str:
    if not data:
        raise DocumentError("Document is empty")
    if media_type == "text/plain":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DocumentError("Text must be UTF-8") from exc
        if "\x00" in text:
            raise DocumentError("Binary content is not accepted as text")
    elif media_type == "application/pdf":
        if not data.startswith(b"%PDF-"):
            raise DocumentError("Invalid PDF signature")
        try:
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted:
                raise DocumentError("Encrypted PDFs are not supported")
            if len(reader.pages) > settings.max_pages:
                raise DocumentError("PDF exceeds the page limit")
            pages = []
            total_chars = 0
            for page in reader.pages:
                page_text = page.extract_text() or ""
                if not page_text.strip():
                    raise DocumentError("PDF has a page without readable text; OCR is required")
                total_chars += len(page_text) + 1
                if total_chars > settings.max_text_chars:
                    raise DocumentError("Extracted text exceeds the character limit")
                pages.append(page_text)
            text = "\n".join(pages)
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError("PDF could not be parsed") from exc
    else:
        raise DocumentError("Only application/pdf and text/plain are supported")
    if not text.strip():
        raise DocumentError("Document has no readable text")
    if len(text) > settings.max_text_chars:
        raise DocumentError("Extracted text exceeds the character limit")
    return text
