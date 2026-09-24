"""Durable SQLite job worker; run one worker process alongside the API."""

import argparse
import time
from datetime import datetime

import httpx

from docureview.config import Settings
from docureview.documents import DocumentError
from docureview.extraction import DemoExtractor, LabelsExtractor, OllamaExtractor, ProviderError
from docureview.processing import process
from docureview.store import Store


def run_once(store, settings, extractor):
    store.heartbeat()
    store.purge()
    job = store.claim()
    if job is None:
        return False
    try:
        document = process(
            job["payload"],
            job["media_type"],
            settings,
            extractor,
            job["tenant"],
            job["owner"],
            datetime.fromisoformat(job["expires_at"]),
        )
        store.finish(job, document)
    except DocumentError:
        store.fail(
            job, "Document could not be parsed; check format, limits and OCR settings", False
        )
    except ProviderError:
        store.fail(job, "Extraction provider failed", True)
    except Exception:
        store.fail(job, "Processing failed; contact the operator", False)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--purge-only", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    store = Store(settings.database)
    store.initialize(settings.retention_days)
    if args.purge_only:
        return
    with httpx.Client(timeout=httpx.Timeout(120, connect=5), trust_env=False) as client:
        extractor = (
            DemoExtractor()
            if settings.provider == "demo"
            else LabelsExtractor()
            if settings.provider == "labels"
            else OllamaExtractor(client, settings.ollama_url, settings.ollama_model)
        )
        while True:
            worked = run_once(store, settings, extractor)
            if args.once:
                return
            if not worked:
                time.sleep(1)


if __name__ == "__main__":
    main()
