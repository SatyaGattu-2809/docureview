# Evaluation protocol and results

The evaluation separates **header extraction**, **OCR**, and **richer invoice validation**.
There is no live-model accuracy claim in this release. Ollama is implemented, but no model
was supplied or running during verification. Automatic acceptance is locked off in the API
and worker, including when an in-memory settings object is changed after validation.

## Frozen header corpus

`evaluation/invoices.json` contains 24 synthetic, explicitly labeled invoices:

| Split | Vendors | Templates | Cases |
| --- | --- | --- | --- |
| Development | Atlas Analytics, Birch Supply | Colon labels, pipe separators | 12 |
| Holdout | Cedar Labs, Dune Studio | Equals separators, multiline values | 12 |

Each vendor has standard values, formatted dates/amounts, ambiguous numeric dates, missing
tax, conflicting totals, and incorrect arithmetic. Both vendor and template groups are
mutually exclusive across splits; the runner checks this before evaluation. No fixture is
included in the extraction prompt. The corpus is a small developer-authored regression set,
not an independently collected sample of production invoices. It is not statistically
sufficient for selecting a safe acceptance threshold.

Labels specify the seven canonical header values. Null means the source does not provide an
unambiguous value. An arithmetically inconsistent invoice retains its observed field values
in the gold labels and is separately marked `must_review`. Correct extraction does not imply
that the invoice is valid. Provider errors count as every field incorrect, including nulls.

### Metrics

- **Field accuracy:** exact canonical matches / all labeled fields (including correct nulls).
- **Full-invoice accuracy:** all seven header labels match, including null labels where required.
  This is extraction accuracy, not accounting validity or successful approval.
- **Policy review rate:** all documents are routed to review in this release (100%).
- **Shadow review rate:** simulated routing with a 0.90 threshold, for policy analysis only.
- **Shadow accepted error fraction:** accepted documents with any incorrect field or a
  `must_review` label / accepted documents. Null means no documents were accepted; it does
  not mean zero risk. Real production accepted-error fraction is undefined because acceptance
  is disabled.

The baseline ran against the original implementation at commit
`58edd8103bafdddd9bebbc10675940cbfb9f196b`, before normalization changes. Reports contain
per-document outcomes and a SHA-256 of the exact dataset bytes.

| Demo-parser metric | Baseline development | Updated development | Baseline holdout | Updated holdout |
| --- | ---: | ---: | ---: | ---: |
| Field accuracy | 48.81% | 53.57% | 7.14% | 7.14% |
| Full-invoice accuracy | 33.33% | 50.00% | 0% | 0% |
| Policy review rate | 100% | 100% | 100% | 100% |
| Shadow review rate | 91.67% | 83.33% | 100% | 100% |
| Shadow accepted count | 1 | 2 | 0 | 0 |
| Errors / shadow accepted | 0 / 1 | 0 / 2 | Undefined | Undefined |

The demo parser was intentionally not taught the held-out layouts. Its 7.14% holdout field
accuracy comes from correctly returning some null labels, not successfully reading unfamiliar
invoices. Normalization improves the supported layout but does not fix generalization. Zero
errors among one or two accepted examples is not evidence that automatic acceptance is safe.

## Reproduce

From an installed checkout:

```bash
python evaluation/run.py --provider demo --output evaluation/reports/current-demo.json
python evaluation/run.py --provider ollama --output /tmp/live-model-report.json
```

The Ollama command requires `OLLAMA_URL` and `OLLAMA_MODEL` (URL defaults to localhost).
Preserve the report with the exact installed model digest, model runtime version, repository
commit, dataset hash, and hardware details. Compare model candidates on development data;
reserve a new independently collected vendor/template holdout for final model selection.
Do not interpret repeated tuning against this published holdout as unbiased generalization.

To rerun the original baseline, install the original commit in an isolated environment and
run this revision's `evaluation/run.py` against that installed package. The runner supports
both the original and updated assessment models. Keep environments separate so the editable
current package does not shadow the baseline package.

`evaluation/build_dataset.py` regenerates the labeled corpus deterministically. Latencies in
reports vary by machine and run; metric counts and labels are deterministic in demo mode.

## Separate OCR track

`evaluation/ocr` has two rasterized English invoices: clear and rotated by two degrees.
Tesseract 5.3.4 produced 0% whitespace-normalized character error and the downstream demo
parser matched 7/7 fields for both images. Both require review regardless of score.
This is a smoke test, not evidence about low-quality scans, handwriting, multiple languages,
tables, or real camera photographs.

```bash
python evaluation/run_ocr.py --output evaluation/reports/ocr-demo.json
```

Requires Tesseract. PDF OCR additionally requires Poppler's `pdftoppm`. Regenerating PNGs
requires Pillow and the DejaVu Sans font at the path in `build_ocr.py`; normal evaluation uses
the committed images and has no Pillow dependency.

## Separate richer-schema track

Four labeled fixtures cover line items, shipping/discount, a wrong quantity/line-amount
relationship, and a subtotal inconsistent with line amounts. The deterministic parser reads
a documented labeled line format; all gold fields match and all four validity decisions are
correct in the recorded run. This checks arithmetic and data flow, not real table extraction.

```bash
python evaluation/run_rich.py --output evaluation/reports/rich-demo.json
```

Before broader deployment, evaluate actual scanned and text invoices separately by vendor,
layout, language, quality and currency. Measure OCR error, field and document accuracy,
false acceptance, reviewer correction rates, p50/p95 latency, and inference resource use.
Pin model digests and prompt versions. Add a regression gate before model/prompt upgrades;
monitor each slice after release. Reviewed corrections require a separate labeling and
consent process before being reused as training or evaluation data.

## Layout and vendor breakdowns

Version 2 header reports add `slices.template` and `slices.vendor`, keeping development
and holdout results separate within each slice. Every slice includes its document count,
per-field and full-invoice accuracy, provider failures, and simulated acceptance errors.
`present_field_accuracy` scores only fields whose gold value is non-null. This complements
all-field accuracy: a parser returning null for everything can match missing labels without
extracting any of the information that is actually present.

The frozen `evaluation/reports/layout-demo.json` records the current deterministic parser:

| Layout | Split | Documents | All-field accuracy | Present-field accuracy | Full-invoice accuracy |
| --- | --- | ---: | ---: | ---: | ---: |
| Colon | Development | 6 | 100% | 100% | 100% |
| Pipe | Development | 6 | 7.14% | 0% | 0% |
| Equals | Holdout | 6 | 7.14% | 0% | 0% |
| Multiline | Holdout | 6 | 7.14% | 0% | 0% |

Each vendor currently has exactly one layout, so this corpus cannot distinguish vendor
effects from layout effects. These are text-layout fixtures, not a benchmark of PDF reading
order or visually complex invoices. OCR remains a separate track. The perfect colon extraction
score includes correctly extracted invoices with invalid arithmetic that still need review.

Reports also include p50/p95 extraction-plus-assessment latency, using nearest-rank percentiles
and including failed calls. These timings exclude file parsing, networking to the application,
and queue wait time. They are informational and vary by machine; CI does not gate on them.

The runner rejects empty datasets, duplicate IDs/documents (ignoring whitespace), incomplete
labels, invalid splits, and vendor/template overlap before making any model calls. Both splits
must be present. It accepts a separate labeled corpus through `--dataset path/to/invoices.json`;
use the committed JSON structure and canonical gold values. Only exception class names are
recorded for failures, never upstream response bodies or document text.

### Regression check

```bash
python evaluation/run.py --provider demo \
  --baseline evaluation/reports/layout-demo.json \
  --output /tmp/current-layout-report.json
```

CI runs this comparison as part of the required `test` job. It fails if field, present-field,
full-invoice, or any individual field accuracy drops overall or in any vendor/layout slice,
or if provider failures or simulated unsafe acceptances increase. A report is still written
when scores regress, so the failed run can be inspected. Baseline and output paths must differ.
Comparisons require matching dataset hashes, provider names, report versions, thresholds,
groups, and denominators. Historical version 1 reports remain available for the original
normalization comparison; they are not inputs to this gate.

This gate protects reproducibility, not production readiness: it deliberately preserves the
weak demo baseline rather than pretending its holdout results are adequate. Review changes
to the baseline explicitly; do not overwrite it automatically to make CI pass. Live Ollama
runs can produce the same breakdowns, but this deterministic gate does not certify live-model
quality or select an acceptance threshold. Automatic acceptance remains disabled.
