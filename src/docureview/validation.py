import re

from pydantic import ValidationError

from docureview.models import (
    FIELD_NAMES,
    Assessment,
    CandidateField,
    Extraction,
    FieldCheck,
    Invoice,
    SourcePage,
    SourceSpan,
)
from docureview.normalization import normalize


def locate(candidate: CandidateField, pages: list[SourcePage]) -> SourceSpan | None:
    """Resolve a unique exact source location; never trust model-provided offsets."""
    raw = candidate.raw_value or candidate.value
    if not raw or not candidate.evidence:
        return None
    matches = []
    for page in pages:
        if candidate.page is not None and candidate.page != page.page:
            continue
        for quote in re.finditer(re.escape(candidate.evidence), page.text):
            for value in re.finditer(
                r"(?<![\w.,])" + re.escape(raw) + r"(?![\w.,])", candidate.evidence
            ):
                start = quote.start() + value.start()
                matches.append(
                    SourceSpan(page=page.page, start=start, end=start + len(raw), text=raw)
                )
    return matches[0] if len(matches) == 1 else None


def check(name: str, candidate: CandidateField, pages: list[SourcePage], currency: str | None):
    kind = name.rsplit(".", 1)[-1]
    value = normalize(kind, candidate.value, currency)
    raw = candidate.raw_value or candidate.value
    source_value = normalize(kind, raw, currency)
    source = locate(candidate, pages)
    valid = value is not None and value == source_value and source is not None
    return FieldCheck(
        field=name,
        score=candidate.confidence if valid else 0,
        evidence_found=source is not None,
        value_found_in_evidence=valid,
        source=source,
        normalized_value=value,
    )


def assess(
    extraction: Extraction, text: str | list[SourcePage], threshold: float, auto_accept: bool
) -> Assessment:
    pages = [SourcePage(page=1, text=text)] if isinstance(text, str) else text
    currency = normalize("currency", extraction.currency.value)
    candidates = {name: getattr(extraction, name) for name in FIELD_NAMES}
    for name in ["shipping", "discount"]:
        if getattr(extraction, name) is not None:
            candidates[name] = getattr(extraction, name)
    for index, item in enumerate(extraction.line_items):
        for name in ["description", "quantity", "unit_price", "amount"]:
            candidates[f"line_items.{index}.{name}"] = getattr(item, name)
    checks = [check(name, candidate, pages, currency) for name, candidate in candidates.items()]
    values = {c.field: c.normalized_value for c in checks}
    issues = []
    for c in checks:
        if c.normalized_value is None:
            issues.append(f"{c.field}: missing or ambiguous value")
        if not c.value_found_in_evidence:
            issues.append(f"{c.field}: source evidence check failed")
        if c.score < threshold:
            issues.append(f"{c.field}: confidence below threshold")
    # Explicitly conflicting labeled fields require review even if the model selected one.
    for name in FIELD_NAMES:
        label = name.replace("_", r"[ \t]+")
        found = []
        for page in pages:
            found.extend(
                re.findall(rf"^{label}[ \t]*[:=|][ \t]*([^\n]+)", page.text, flags=re.I | re.M)
            )
        normalized = {normalize(name, raw, currency) for raw in found}
        if len(normalized) > 1:
            issues.append(f"{name}: conflicting source values")
    invoice_values = {k: v for k, v in values.items() if not k.startswith("line_items.")}
    invoice_values["line_items"] = [
        {
            name: values[f"line_items.{index}.{name}"]
            for name in ["description", "quantity", "unit_price", "amount"]
        }
        for index in range(len(extraction.line_items))
    ]
    try:
        invoice = Invoice.model_validate(invoice_values)
    except ValidationError as exc:
        invoice = None
        for error in exc.errors():
            location = ".".join(str(item) for item in error["loc"]) or "invoice totals"
            issues.append(f"{location}: {error['type']}")
    if any(page.method == "ocr" for page in pages):
        issues.append("OCR output requires human verification against the original")
    if not auto_accept:
        issues.append("Automatic acceptance is disabled; human review is required")
    return Assessment(
        invoice=invoice,
        confidence=min(c.score for c in checks),
        field_checks=checks,
        normalized_values=values,
        issues=issues,
        status="needs_review" if issues else "auto_accepted",
    )
