"""Internal isolated parser. No server imports and no network access is needed."""

import json
import os
import subprocess
import sys
from pathlib import Path


def ocr(path: Path) -> str:
    output = path.parent / "recognized"
    subprocess.run(
        ["tesseract", str(path), str(output), "-l", "eng", "--psm", "3"],
        check=True,
        timeout=25,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if output.with_suffix(".txt").stat().st_size > 100_000:
        raise ValueError("OCR text exceeds the output limit")
    return output.with_suffix(".txt").read_text()


def parse(source: Path, media_type: str, max_pages: int, max_chars: int, use_ocr: bool):
    from pypdf import PdfReader

    data = source.read_bytes()
    pages = []
    if media_type == "text/plain":
        text = data.decode("utf-8-sig")
        if "\x00" in text:
            raise ValueError("Binary content is not accepted as text")
        pages.append({"page": 1, "text": text, "method": "text"})
    elif media_type in {"image/png", "image/jpeg"}:
        signature = b"\x89PNG\r\n\x1a\n" if media_type == "image/png" else b"\xff\xd8\xff"
        if not data.startswith(signature):
            raise ValueError("Invalid image signature")
        if not use_ocr:
            raise ValueError("OCR is disabled")
        pages.append({"page": 1, "text": ocr(source), "method": "ocr"})
    elif media_type == "application/pdf":
        if not data.startswith(b"%PDF-"):
            raise ValueError("Invalid PDF signature")
        reader = PdfReader(source)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are not supported")
        if len(reader.pages) > max_pages:
            raise ValueError("PDF exceeds the page limit")
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            method = "pdf_text"
            if not text.strip():
                if not use_ocr:
                    raise ValueError("PDF has a page without readable text; OCR is required")
                prefix = source.parent / "page"
                subprocess.run(
                    [
                        "pdftoppm",
                        "-f",
                        str(number),
                        "-l",
                        str(number),
                        "-singlefile",
                        "-scale-to",
                        "2400",
                        "-png",
                        str(source),
                        str(prefix),
                    ],
                    check=True,
                    timeout=20,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                text, method = ocr(prefix.with_suffix(".png")), "ocr"
            pages.append({"page": number, "text": text, "method": method})
            if sum(len(p["text"]) for p in pages) > max_chars:
                raise ValueError("Extracted text exceeds the character limit")
    else:
        raise ValueError("Unsupported media type")
    if not pages or not any(page["text"].strip() for page in pages):
        raise ValueError("Document has no readable text")
    if sum(len(p["text"]) for p in pages) > max_chars:
        raise ValueError("Extracted text exceeds the character limit")
    return pages


def main():
    # Limits apply to this child and its rasterizer/OCR children on Linux.
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
        resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
    os.environ["OMP_THREAD_LIMIT"] = "1"
    try:
        pages = parse(
            Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5] == "1"
        )
        print(json.dumps({"pages": pages}))
    except UnicodeDecodeError:
        print(json.dumps({"error": "Text must be UTF-8"}))
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
    except FileNotFoundError:
        print(json.dumps({"error": "OCR requires tesseract and pdftoppm on the server"}))
    except Exception:
        print(json.dumps({"error": "Document could not be parsed"}))


if __name__ == "__main__":
    main()
