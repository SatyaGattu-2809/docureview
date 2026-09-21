"""Separate OCR + demo-extraction smoke evaluation, not a scan-quality benchmark."""

import argparse
import json
import subprocess
from pathlib import Path

from docureview.config import Settings
from docureview.documents import read_pages
from docureview.extraction import DemoExtractor
from docureview.models import FIELD_NAMES
from docureview.validation import assess


def edit_distance(a, b):
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).parent
    settings = Settings(
        api_key="ocr-evaluation-upload", reviewer_key="ocr-evaluation-review", ocr_enabled=True
    )
    rows = []
    for case in json.loads((root / "ocr/labels.json").read_text()):
        pages = read_pages((root / case["path"]).read_bytes(), "image/png", settings)
        text = "\n".join(p.text for p in pages)
        extraction = DemoExtractor().extract(text)
        result = assess(extraction, pages, 0.9, True)
        actual, expected = " ".join(text.split()), " ".join(case["text"].split())
        rows.append(
            {
                "id": case["id"],
                "character_error_rate": edit_distance(expected, actual) / len(expected),
                "field_accuracy": sum(
                    result.normalized_values[f] == case["expected"][f] for f in FIELD_NAMES
                )
                / len(FIELD_NAMES),
                "review_required": result.status == "needs_review",
            }
        )
    report = {
        "track": "ocr",
        "fixtures": "two synthetic printed English images",
        "provider": "Tesseract + deterministic demo parser; no LLM",
        "tesseract": subprocess.check_output(["tesseract", "--version"], text=True).splitlines()[0],
        "rows": rows,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
