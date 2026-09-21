"""Keep evaluation failures visible instead of hiding them in aggregate scores."""

import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from docureview.extraction import DemoExtractor, ProviderError

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("header_evaluation", ROOT / "evaluation/run.py")
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


@pytest.fixture
def cases():
    return json.loads((ROOT / "evaluation/invoices.json").read_text())


@pytest.mark.parametrize(
    "problem",
    [
        "empty",
        "missing_label",
        "duplicate_id",
        "duplicate_text",
        "vendor_overlap",
        "template_overlap",
        "missing_holdout",
        "bad_split",
        "bad_review_label",
    ],
)
def test_invalid_dataset_fails_before_inference(cases, problem):
    if problem == "empty":
        cases = []
    elif problem == "missing_label":
        del cases[0]["expected"]["tax"]
    elif problem == "duplicate_id":
        cases[1]["id"] = cases[0]["id"]
    elif problem == "duplicate_text":
        cases[-1]["text"] = cases[0]["text"].replace("\n", "  ")
    elif problem in {"vendor_overlap", "template_overlap"}:
        key = problem.removesuffix("_overlap")
        cases[-1][key] = " " + cases[0][key].upper() + " "
    elif problem == "missing_holdout":
        cases = [c for c in cases if c["split"] == "dev"]
    elif problem == "bad_split":
        cases[-1]["split"] = "test"
    else:
        cases[0]["must_review"] = "false"

    class NeverCalled:
        def extract(self, text):
            pytest.fail("Invalid data must be rejected before contacting the model")

    with pytest.raises(ValueError):
        evaluation.run(cases, NeverCalled())


def test_missing_predictions_do_not_inflate_present_field_accuracy(cases):
    report = evaluation.run(cases, DemoExtractor())
    for template in ("equals", "multiline"):
        metrics = report["slices"]["template"][f"headers/holdout/{template}"]
        assert metrics["documents"] == 6
        assert metrics["field_accuracy"] == pytest.approx(3 / 42)
        assert metrics["present_fields"] == 39
        assert metrics["present_field_accuracy"] == 0
        assert metrics["shadow_accepted_error_fraction"] is None


def test_provider_failure_is_wrong_even_for_null_labels(cases):
    class Unavailable:
        name = "unavailable"

        def extract(self, text):
            raise ProviderError("Do not write this or the document text into reports")

    report = evaluation.run(cases, Unavailable())
    for metrics in report["metrics"].values():
        assert metrics["field_accuracy"] == 0
        assert metrics["present_field_accuracy"] == 0
        assert metrics["provider_failures"] == metrics["documents"]
    assert all(row["error"] == "ProviderError" for row in report["rows"])
    assert "Do not write" not in json.dumps(report)


def test_latency_uses_nearest_rank_percentiles(cases):
    row = evaluation.run(cases, DemoExtractor())["rows"][0]
    rows = [dict(row, latency_seconds=i) for i in range(1, 21)]
    metrics = evaluation.summarize(rows)
    assert metrics["latency_p50_seconds"] == 10
    assert metrics["latency_p95_seconds"] == 19


def test_regression_gate_catches_layout_drop_when_overall_scores_are_unchanged(cases):
    baseline = evaluation.run(cases, DemoExtractor())
    baseline["dataset_sha256"] = "same-dataset"
    candidate = copy.deepcopy(baseline)
    assert evaluation.compare_reports(candidate, baseline) == []
    candidate["slices"]["template"]["headers/dev/colon"]["per_field_accuracy"]["total"] = 0
    failures = evaluation.compare_reports(candidate, baseline)
    assert any("template/headers/dev/colon: total fell" in failure for failure in failures)


def test_regression_gate_rejects_different_datasets(cases):
    baseline = evaluation.run(cases, DemoExtractor())
    baseline["dataset_sha256"] = "original"
    candidate = dict(baseline, dataset_sha256="modified")
    with pytest.raises(ValueError, match="dataset_sha256"):
        evaluation.compare_reports(candidate, baseline)


def test_regression_gate_catches_new_unsafe_acceptance(cases):
    baseline = evaluation.run(cases, DemoExtractor())
    baseline["dataset_sha256"] = "same-dataset"
    candidate = copy.deepcopy(baseline)
    candidate["metrics"]["headers/holdout"]["shadow_accepted_errors"] += 1
    assert any(
        "shadow_accepted_errors" in f for f in evaluation.compare_reports(candidate, baseline)
    )


def test_cli_preserves_baseline_and_exits_nonzero_for_regression(tmp_path, cases):
    baseline = evaluation.run(cases, DemoExtractor())
    import hashlib

    baseline["dataset_sha256"] = hashlib.sha256(
        (ROOT / "evaluation/invoices.json").read_bytes()
    ).hexdigest()
    # A hypothetical stronger baseline must reject today's weaker parser.
    baseline["metrics"]["headers/holdout"]["field_accuracy"] = 1.0
    path = tmp_path / "baseline.json"
    original = json.dumps(baseline)
    path.write_text(original)
    output = tmp_path / "actual.json"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    command = [sys.executable, str(ROOT / "evaluation/run.py"), "--baseline", str(path)]
    result = subprocess.run(
        command + ["--output", str(output)], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 1
    assert "Evaluation regression" in result.stderr
    assert json.loads(output.read_text())["report_version"] == 2
    assert path.read_text() == original
    result = subprocess.run(
        command + ["--output", str(path)], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 2
    assert path.read_text() == original
