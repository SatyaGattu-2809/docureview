from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, max_length=300)]
Money = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2)]
FieldName = Literal[
    "invoice_number", "vendor", "invoice_date", "currency", "subtotal", "tax", "total"
]
FIELD_NAMES = ("invoice_number", "vendor", "invoice_date", "currency", "subtotal", "tax", "total")
Status = Literal["needs_review", "auto_accepted", "approved", "rejected"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Invoice(Model):
    invoice_number: Text
    vendor: Text
    invoice_date: date
    currency: Literal["USD", "EUR", "GBP", "INR", "CAD", "AUD"]
    subtotal: Money
    tax: Money
    total: Money

    @model_validator(mode="after")
    def reconcile(self):
        if self.subtotal + self.tax != self.total:
            raise ValueError("subtotal + tax must equal total")
        return self


class CandidateField(Model):
    value: str | None = Field(max_length=300)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence: str | None = Field(max_length=1000)


class Extraction(Model):
    invoice_number: CandidateField
    vendor: CandidateField
    invoice_date: CandidateField
    currency: CandidateField
    subtotal: CandidateField
    tax: CandidateField
    total: CandidateField


class FieldCheck(Model):
    field: FieldName
    score: float
    evidence_found: bool
    value_found_in_evidence: bool


class Assessment(Model):
    invoice: Invoice | None
    confidence: float
    field_checks: list[FieldCheck]
    issues: list[str]
    status: Literal["needs_review", "auto_accepted"]


class ReviewRequest(Model):
    expected_version: int = Field(ge=1)
    decision: Literal["approved", "rejected"]
    corrected_invoice: Invoice | None = None
    note: str = Field(default="", max_length=2000)


class ReviewEvent(Model):
    reviewer: str
    decision: Literal["approved", "rejected"]
    note: str
    at: datetime
    version: int


class Document(Model):
    id: str
    sha256: str
    created_at: datetime
    provider: str
    status: Status
    version: int = 1
    extraction: Extraction
    assessment: Assessment
    final_invoice: Invoice | None
    reviews: list[ReviewEvent] = Field(default_factory=list)
