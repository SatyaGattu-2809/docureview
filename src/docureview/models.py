from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
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


class LineItem(Model):
    description: Text
    quantity: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=3)]
    unit_price: Money
    amount: Money

    @model_validator(mode="after")
    def reconcile(self):
        expected = (self.quantity * self.unit_price).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if expected != self.amount:
            raise ValueError("quantity * unit_price must equal line amount")
        return self


class Invoice(Model):
    invoice_number: Text
    vendor: Text
    invoice_date: date
    currency: Literal["USD", "EUR", "GBP", "INR", "CAD", "AUD"]
    subtotal: Money
    tax: Money
    total: Money

    shipping: Money = Decimal("0.00")
    discount: Money = Decimal("0.00")
    line_items: list[LineItem] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def reconcile(self):
        if self.line_items and sum(item.amount for item in self.line_items) != self.subtotal:
            raise ValueError("line amounts must sum to subtotal")
        if self.subtotal + self.tax + self.shipping - self.discount != self.total:
            raise ValueError("subtotal + tax + shipping - discount must equal total")
        return self


class CandidateField(Model):
    value: str | None = Field(max_length=300)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence: str | None = Field(max_length=1000)
    raw_value: str | None = Field(default=None, max_length=300)
    page: int | None = Field(default=None, ge=1)


class ExtractedLineItem(Model):
    description: CandidateField
    quantity: CandidateField
    unit_price: CandidateField
    amount: CandidateField


class Extraction(Model):
    invoice_number: CandidateField
    vendor: CandidateField
    invoice_date: CandidateField
    currency: CandidateField
    subtotal: CandidateField
    tax: CandidateField
    total: CandidateField
    shipping: CandidateField | None = None
    discount: CandidateField | None = None
    line_items: list[ExtractedLineItem] = Field(default_factory=list, max_length=100)


class SourcePage(Model):
    page: int
    text: str
    method: Literal["text", "pdf_text", "ocr"] = "text"


class SourceSpan(Model):
    page: int
    start: int
    end: int
    text: str


class FieldCheck(Model):
    field: str
    score: float
    evidence_found: bool
    value_found_in_evidence: bool
    source: SourceSpan | None = None
    normalized_value: str | None = None


class Assessment(Model):
    invoice: Invoice | None
    confidence: float
    field_checks: list[FieldCheck]
    issues: list[str]
    normalized_values: dict[str, str | None] = Field(default_factory=dict)
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
    tenant: str = "local"
    owner: str = "local-uploader"
    expires_at: datetime | None = None
    source_pages: list[SourcePage] = Field(default_factory=list)
    media_type: str = "text/plain"
    ocr_used: bool = False
    status: Status
    version: int = 1
    extraction: Extraction
    assessment: Assessment
    final_invoice: Invoice | None
    reviews: list[ReviewEvent] = Field(default_factory=list)
