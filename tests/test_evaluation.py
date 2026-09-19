import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from docureview.config import Settings
from docureview.documents import read_pages
from docureview.extraction import DemoExtractor
from docureview.validation import assess

ROOT = Path(__file__).parents[1] / "evaluation"


def test_dataset_has_disjoint_vendor_and_template_groups():
    cases = json.loads((ROOT / "invoices.json").read_text())
    assert len(cases) == 24
    for name in ["vendor", "template"]:
        dev = {case[name] for case in cases if case["split"] == "dev"}
        heldout = {case[name] for case in cases if case["split"] == "holdout"}
        assert not dev & heldout


def test_evaluation_undefined_accepted_error_rate():
    spec = importlib.util.spec_from_file_location("evaluate", ROOT / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = json.loads((ROOT / "invoices.json").read_text())
    report = module.run(cases, DemoExtractor())
    holdout = report["metrics"]["headers/holdout"]
    assert holdout["shadow_auto_accepted"] == 0
    assert holdout["shadow_accepted_error_fraction"] is None
    assert holdout["policy_review_rate"] == 1


@pytest.mark.parametrize(
    "case", json.loads((ROOT / "rich.json").read_text()), ids=lambda c: c["id"]
)
def test_rich_track(case):
    result = assess(DemoExtractor().extract(case["text"]), case["text"], 0.9, False)
    assert (result.invoice is not None) == case["valid_invoice"]
    for field, value in case["expected"].items():
        assert result.normalized_values[field] == value
    for check in result.field_checks:
        assert check.source is not None


@pytest.mark.skipif(not shutil.which("tesseract"), reason="Optional Tesseract binary not installed")
@pytest.mark.parametrize(
    "case", json.loads((ROOT / "ocr/labels.json").read_text()), ids=lambda c: c["id"]
)
def test_real_ocr_pipeline(case):
    settings = Settings(
        api_key="ocr-test-upload-123", reviewer_key="ocr-test-review-123", ocr_enabled=True
    )
    pages = read_pages((ROOT / case["path"]).read_bytes(), "image/png", settings)
    assert pages[0].method == "ocr"
    result = assess(DemoExtractor().extract(pages[0].text), pages, 0.9, True)
    assert result.status == "needs_review"
    for name, value in case["expected"].items():
        assert result.normalized_values[name] == value
