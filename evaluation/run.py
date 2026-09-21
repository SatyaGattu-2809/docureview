"""Measure extraction independently of routing. Demo results are not LLM accuracy."""

import argparse
import hashlib
import json
import math
import os
import time
from collections import defaultdict
from pathlib import Path

import httpx

from docureview.extraction import DemoExtractor, OllamaExtractor
from docureview.models import FIELD_NAMES
from docureview.validation import assess


def validate_cases(cases):
    """Reject broken labels and split leakage before calling an extraction provider."""
    if not isinstance(cases, list) or not cases:
        raise ValueError("Dataset must be a non-empty list")
    ids, texts = set(), set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Each case must be an object")
        for key in ("id", "vendor", "template", "text", "track"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                raise ValueError(f"Each case needs a non-empty {key}")
        if case.get("split") not in {"dev", "holdout"} or case["track"] != "headers":
            raise ValueError("Header cases must use the dev or holdout split")
        if type(case.get("must_review")) is not bool:
            raise ValueError("must_review must be a boolean")
        expected = case.get("expected")
        if not isinstance(expected, dict) or set(expected) != set(FIELD_NAMES):
            raise ValueError("Gold labels must contain exactly the seven header fields")
        if any(
            v is not None and (not isinstance(v, str) or not v.strip()) for v in expected.values()
        ):
            raise ValueError("Gold values must be non-empty strings or null")
        text = " ".join(case["text"].split())
        if case["id"] in ids or text in texts:
            raise ValueError("Duplicate case ID or document text")
        ids.add(case["id"])
        texts.add(text)
    dev = [c for c in cases if c["split"] == "dev"]
    holdout = [c for c in cases if c["split"] == "holdout"]
    if not dev or not holdout:
        raise ValueError("Both development and holdout cases are required")
    for group in ("vendor", "template"):
        if {c[group].strip().casefold() for c in dev} & {
            c[group].strip().casefold() for c in holdout
        }:
            raise ValueError(f"Development and holdout overlap on {group}")


def summarize(rows):
    n = len(rows)
    accepted = [r for r in rows if r["shadow_accepted"]]
    present = sum(r["present_fields"] for r in rows)
    latencies = sorted(r["latency_seconds"] for r in rows)
    return {
        "documents": n,
        "field_accuracy": sum(r["correct_fields"] for r in rows) / (n * len(FIELD_NAMES)),
        "full_invoice_accuracy": sum(r["correct_fields"] == len(FIELD_NAMES) for r in rows) / n,
        "per_field_accuracy": {
            f: sum(r["field_correct"][f] for r in rows) / n for f in FIELD_NAMES
        },
        "present_fields": present,
        "present_field_accuracy": (
            sum(r["correct_present_fields"] for r in rows) / present if present else None
        ),
        "latency_p50_seconds": latencies[math.ceil(n * 0.50) - 1],
        "latency_p95_seconds": latencies[math.ceil(n * 0.95) - 1],
        "shadow_accepted_errors": sum(r["incorrect_acceptance"] for r in accepted),
        "policy_review_rate": 1.0,
        "shadow_review_rate": 1 - len(accepted) / n,
        "shadow_auto_accepted": len(accepted),
        "shadow_accepted_error_fraction": (
            sum(r["incorrect_acceptance"] for r in accepted) / len(accepted) if accepted else None
        ),
        "provider_failures": sum(r["error"] is not None for r in rows),
    }


def run(cases, extractor):
    validate_cases(cases)
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
                "vendor": case["vendor"],
                "template": case["template"],
                "present_fields": sum(case["expected"][f] is not None for f in FIELD_NAMES),
                "correct_present_fields": sum(
                    correct[f] and case["expected"][f] is not None for f in FIELD_NAMES
                ),
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
    slices = {}
    for dimension in ("vendor", "template"):
        sliced = defaultdict(list)
        for row in rows:
            sliced[f"{row['track']}/{row['split']}/{row[dimension]}"].append(row)
        slices[dimension] = {key: summarize(group) for key, group in sorted(sliced.items())}
    return {
        "report_version": 2,
        "provider": extractor.name,
        "dataset_type": "synthetic; not production evidence",
        "threshold": 0.9,
        "automatic_acceptance_enabled": False,
        "metrics": {key: summarize(group) for key, group in groups.items()},
        "slices": slices,
        "rows": rows,
    }


def compare_reports(report, baseline):
    """Compare like-for-like demo runs; latency is informational, never a CI gate."""
    for key in ("report_version", "dataset_sha256", "provider", "threshold"):
        if key not in report or key not in baseline or report[key] != baseline[key]:
            raise ValueError(f"Cannot compare reports with different or missing {key}")
    if report["report_version"] != 2:
        raise ValueError("Regression checks require version 2 reports")
    if report["provider"] != "demo:labels-v1":
        raise ValueError("This deterministic regression gate only supports the demo parser")
    if report.get("automatic_acceptance_enabled") is not False:
        raise ValueError("Automatic acceptance must remain disabled")
    failures = []
    groups = [("metrics", report["metrics"], baseline["metrics"])]
    for dimension in ("vendor", "template"):
        groups.append((dimension, report["slices"][dimension], baseline["slices"][dimension]))
    for dimension, current, previous in groups:
        if current.keys() != previous.keys():
            raise ValueError(f"Cannot compare different {dimension} groups")
        for name, metrics in current.items():
            old = previous[name]
            if any(metrics[k] != old[k] for k in ("documents", "present_fields")):
                raise ValueError(f"Changed metric denominators for {name}")
            scores = {
                k: metrics[k]
                for k in ("field_accuracy", "full_invoice_accuracy", "present_field_accuracy")
            }
            old_scores = {k: old[k] for k in scores}
            scores.update(metrics["per_field_accuracy"])
            old_scores.update(old["per_field_accuracy"])
            for metric, value in scores.items():
                before = old_scores[metric]
                if value is not None and before is not None and value < before:
                    failures.append(f"{dimension}/{name}: {metric} fell from {before} to {value}")
            for metric in ("provider_failures", "shadow_accepted_errors"):
                if metrics[metric] > old[metric]:
                    failures.append(f"{dimension}/{name}: {metric} increased")
    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["demo", "ollama"], default="demo")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=Path(__file__).with_name("invoices.json"))
    parser.add_argument(
        "--baseline", type=Path, help="Version 2 demo report to check for regressions"
    )
    args = parser.parse_args()
    if args.baseline and args.baseline.resolve() == args.output.resolve():
        parser.error("Baseline and output must be different files")
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    dataset = args.dataset.read_bytes()
    cases = json.loads(dataset)
    validate_cases(cases)
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
    if baseline is not None:
        try:
            failures = compare_reports(report, baseline)
        except ValueError as exc:
            parser.error(str(exc))
        if failures:
            parser.exit(1, "Evaluation regression:\n" + "\n".join(failures) + "\n")


if __name__ == "__main__":
    main()
