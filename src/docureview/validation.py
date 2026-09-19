from pydantic import ValidationError

from docureview.models import FIELD_NAMES, Assessment, Extraction, FieldCheck, Invoice


def assess(extraction: Extraction, text: str, threshold: float, auto_accept: bool) -> Assessment:
    issues = []
    checks = []
    values = {}
    for name in FIELD_NAMES:
        candidate = getattr(extraction, name)
        values[name] = candidate.value
        evidence_found = bool(candidate.evidence and candidate.evidence in text)
        value_found = bool(
            candidate.value
            and candidate.evidence
            and candidate.value.casefold() in candidate.evidence.casefold()
        )
        score = candidate.confidence if evidence_found and value_found else 0.0
        checks.append(
            FieldCheck(
                field=name,
                score=score,
                evidence_found=evidence_found,
                value_found_in_evidence=value_found,
            )
        )
        if candidate.value is None or not candidate.value.strip():
            issues.append(f"{name}: missing value")
        if not evidence_found or not value_found:
            issues.append(f"{name}: exact evidence check failed")
        if score < threshold:
            issues.append(f"{name}: confidence below threshold")
    try:
        invoice = Invoice.model_validate(values)
    except ValidationError as exc:
        invoice = None
        # Pydantic errors can include the original values; retain only locations and types.
        for error in exc.errors():
            location = ".".join(str(item) for item in error["loc"]) or "invoice totals"
            issues.append(f"{location}: {error['type']}")
    if not auto_accept:
        issues.append("Automatic acceptance is disabled; human review is required")
    return Assessment(
        invoice=invoice,
        confidence=min(check.score for check in checks),
        field_checks=checks,
        issues=issues,
        status="needs_review" if issues else "auto_accepted",
    )
