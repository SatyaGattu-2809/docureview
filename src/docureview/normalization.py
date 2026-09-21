"""Conservative normalization: ambiguous representations require review."""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

CURRENCIES = {"USD", "EUR", "GBP", "INR", "CAD", "AUD"}
MONTHS = {
    name: i
    for i, name in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        1,
    )
}
MONTHS.update({name[:3]: number for name, number in list(MONTHS.items())})


def normalize_date(raw: str) -> str:
    text = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text).isoformat()
    match = re.fullmatch(r"([A-Za-z]+) (\d{1,2}),? (\d{4})", text)
    if match:
        month, day, year = match.groups()
        return date(int(year), MONTHS[month.lower()], int(day)).isoformat()
    match = re.fullmatch(r"(\d{1,2}) ([A-Za-z]+) (\d{4})", text)
    if match:
        day, month, year = match.groups()
        return date(int(year), MONTHS[month.lower()], int(day)).isoformat()
    match = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", text)
    if match:
        first, second, year = map(int, match.groups())
        if first <= 12 and second <= 12 and first != second:
            raise ValueError("Ambiguous numeric date")
        day, month = (first, second) if first > 12 else (second, first)
        return date(year, month, day).isoformat()
    raise ValueError("Unsupported date representation")


def normalize_money(raw: str, currency: str | None = None) -> str:
    text = raw.strip()
    # Only explicitly known currency codes/symbols may be removed.
    codes = re.findall(r"\b[A-Z]{3}\b", text)
    if codes and (currency is None or any(code != currency for code in codes)):
        raise ValueError("Conflicting currency")
    for code in CURRENCIES:
        text = re.sub(rf"\b{code}\b", "", text).strip()
    for symbol, supported in {
        "$": {"USD", "CAD", "AUD"},
        "€": {"EUR"},
        "£": {"GBP"},
        "₹": {"INR"},
    }.items():
        if symbol in text:
            if currency not in supported:
                raise ValueError("Currency symbol conflicts with explicit currency")
            text = text.replace(symbol, "").strip()
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?", text):
        text = text.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", text):
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+,\d{1,2}", text):
        text = text.replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", text):
        raise ValueError("Unsupported monetary representation")
    amount = Decimal(text)
    if amount > Decimal("999999999999.99"):
        raise ValueError("Amount too large")
    return format(amount.quantize(Decimal("0.01")), "f")


def normalize(name: str, raw: str | None, currency: str | None = None) -> str | None:
    if raw is None or not raw.strip():
        return None
    try:
        if name == "invoice_date":
            return normalize_date(raw)
        if name == "currency":
            value = raw.strip().upper()
            if value not in CURRENCIES:
                raise ValueError("Explicit supported currency code required")
            return value
        if name in {"subtotal", "tax", "total", "shipping", "discount", "unit_price", "amount"}:
            return normalize_money(raw, currency)
        if name == "quantity":
            if not re.fullmatch(r"\d+(?:\.\d{1,3})?", raw.strip()):
                raise ValueError("Invalid quantity")
            return str(Decimal(raw).normalize())
        return " ".join(raw.split())
    except (ValueError, KeyError, InvalidOperation):
        return None
