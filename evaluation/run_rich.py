"""Separate deterministic richer-schema extraction/validation evaluation."""

import argparse
import json
from pathlib import Path

from docureview.extraction import DemoExtractor
from docureview.validation import assess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(Path(__file__).with_name("rich.json").read_text())
    rows = []
    for case in cases:
        result = assess(DemoExtractor().extract(case["text"]), case["text"], 0.9, False)
        expected = case["expected"]
        accuracy = sum(
            result.normalized_values.get(key) == value for key, value in expected.items()
        ) / len(expected)
        rows.append(
            {
                "id": case["id"],
                "field_accuracy": accuracy,
                "invoice_validation_correct": (result.invoice is not None) == case["valid_invoice"],
                "review_required": result.status == "needs_review",
            }
        )
    report = {
        "track": "rich invoices",
        "provider": "deterministic demo parser, no LLM",
        "scope": "labeled line syntax; schema/validation coverage, not layout generalization",
        "rows": rows,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
