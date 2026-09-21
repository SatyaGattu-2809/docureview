"""Run PDF/OCR parsing in a bounded child process, outside the API process."""

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from docureview.config import Settings
from docureview.models import SourcePage


class DocumentError(ValueError):
    pass


def read_pages(data: bytes, media_type: str, settings: Settings) -> list[SourcePage]:
    if not data:
        raise DocumentError("Document is empty")
    if len(data) > settings.max_upload_bytes:
        raise DocumentError("Upload exceeds the size limit")
    with tempfile.TemporaryDirectory(prefix="docureview-") as directory:
        source = Path(directory) / "source"
        source.write_bytes(data)
        command = [
            sys.executable,
            "-m",
            "docureview.parse_worker",
            str(source),
            media_type,
            str(settings.max_pages),
            str(settings.max_text_chars),
            "1" if settings.ocr_enabled else "0",
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
        try:
            output, _ = process.communicate(timeout=settings.parse_timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.communicate()
            raise DocumentError("Document parsing exceeded the time limit") from exc
        if process.returncode:
            raise DocumentError("Document parser failed or exceeded resource limits")
        try:
            result = json.loads(output)
            if "error" in result:
                raise DocumentError(result["error"])
            return [SourcePage.model_validate(page) for page in result["pages"]]
        except (ValueError, KeyError) as exc:
            if isinstance(exc, DocumentError):
                raise
            raise DocumentError("Document parser returned an invalid result") from exc


def read_document(data: bytes, media_type: str, settings: Settings) -> str:
    return "\n".join(page.text for page in read_pages(data, media_type, settings))
