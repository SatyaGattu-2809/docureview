"""Measure extraction independently of routing. Demo results are not LLM accuracy."""

import argparse
import hashlib
import json
import os
import time
from collections import defaultdict
from pathlib import Path

import httpx

from docureview.extraction import DemoExtractor, OllamaExtractor
from docureview.models import FIELD_NAMES
from docureview.validation import assess


def summarize(rows):
    n = len(rows)
    accepted = [r for r in rows if r["shadow_accepted"]]
    return {
        "documents": n,
        "field_accuracy": sum(r["correct_fields"] for r in rows) / (n * len(FIELD_NAMES)),
        "full_invoice_accuracy": sum(r["correct_fields"] == len(FIELD_NAMES) for r in rows) / n,
        "per_field_accuracy": {
            f: sum(r["field_correct"][f] for r in rows) / n for f in FIELD_NAMES
        },
        "policy_review_rate": 1.0,
        "shadow_review_rate": 1 - len(accepted) / n,
        "shadow_auto_accepted": len(accepted),
        "shadow_accepted_error_fraction": (
            sum(r["incorrect_acceptance"] for r in accepted) / len(accepted) if accepted else None
        ),
        "provider_failures": sum(r["error"] is not None for r in rows),
    }


def run(cases, extractor):
    rows = []
    for case in cases:
        started = time.monotonic()
        error = None
        try:
            extraction = extractor.extract(case["text"])
            result = assess(extraction, case["text"], 0.9, True)
            values = getattr(result, "normalized_values", None)
            if values is None:
                values = {f: getattr(extraction, f).value for f in FIELD_NAMES}
            correct = {f: values.get(f) == case["expected"][f] for f in FIELD_NAMES}
            accepted = result.status == "auto_accepted"
        except Exception as exc:
            # A failed call counts as wrong even if a gold field is null.
            error = type(exc).__name__
            correct = dict.fromkeys(FIELD_NAMES, False)
            accepted = False
        rows.append(
            {
                "id": case["id"],
                "split": case["split"],
                "track": case["track"],
                "field_correct": correct,
                "correct_fields": sum(correct.values()),
                "shadow_accepted": accepted,
                "incorrect_acceptance": case["must_review"] or not all(correct.values()),
                "error": error,
                "latency_seconds": round(time.monotonic() - started, 4),
            }
        )
    groups = defaultdict(list)
    for row in rows:
        groups[f"{row['track']}/{row['split']}"].append(row)
    return {
        "provider": extractor.name,
        "dataset_type": "synthetic; not production evidence",
        "threshold": 0.9,
        "automatic_acceptance_enabled": False,
        "metrics": {key: summarize(group) for key, group in groups.items()},
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["demo", "ollama"], default="demo")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dataset = Path(__file__).with_name("invoices.json").read_bytes()
    cases = json.loads(dataset)
    dev = [c for c in cases if c["split"] == "dev"]
    holdout = [c for c in cases if c["split"] == "holdout"]
    for group in ["vendor", "template"]:
        assert not {c[group] for c in dev} & {c[group] for c in holdout}, group
    with httpx.Client(timeout=120, trust_env=False) as client:
        extractor = (
            DemoExtractor()
            if args.provider == "demo"
            else OllamaExtractor(
                client,
                os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"),
                os.environ["OLLAMA_MODEL"],
            )
        )
        report = run(cases, extractor)
    report["dataset_sha256"] = hashlib.sha256(dataset).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["metrics"], indent=2))


if __name__ == "__main__":
    main()
