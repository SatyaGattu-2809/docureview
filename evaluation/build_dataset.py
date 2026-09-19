"""Create a deterministic synthetic corpus. Never use these documents as model few-shots."""

import json
from pathlib import Path

ROOT = Path(__file__).parent
FIELDS = ["invoice_number", "vendor", "invoice_date", "currency", "subtotal", "tax", "total"]
VENDORS = [
    ("dev", "Atlas Analytics", "colon"),
    ("dev", "Birch Supply", "pipe"),
    ("holdout", "Cedar Labs", "equals"),
    ("holdout", "Dune Studio", "multiline"),
]


def main():
    cases = []
    for split, vendor, template in VENDORS:
        for scenario in [
            "standard",
            "formatted",
            "ambiguous_date",
            "missing_tax",
            "conflicting_total",
            "bad_arithmetic",
        ]:
            expected = dict(
                zip(
                    FIELDS,
                    ["INV-101", vendor, "2026-09-01", "USD", "1000.00", "80.00", "1080.00"],
                    strict=True,
                )
            )
            values = expected.copy()
            if scenario == "formatted":
                values.update(
                    invoice_date="September 1, 2026", subtotal="1,000.00", total="1,080.00"
                )
            if scenario == "ambiguous_date":
                values["invoice_date"] = "09/01/2026"
                expected["invoice_date"] = None
            if scenario == "missing_tax":
                values.pop("tax")
                expected["tax"] = None
            if scenario == "conflicting_total":
                expected["total"] = None
            if scenario == "bad_arithmetic":
                values["total"] = expected["total"] = "1090.00"
            separators = {"colon": ": ", "pipe": " | ", "equals": " = ", "multiline": "\n"}
            text = (
                "\n".join(
                    k.replace("_", " ").title() + separators[template] + v
                    for k, v in values.items()
                )
                + "\n"
            )
            if scenario == "conflicting_total":
                text += "Total" + separators[template] + "1200.00\n"
            cases.append(
                {
                    "id": f"{template}-{scenario}",
                    "split": split,
                    "vendor": vendor,
                    "template": template,
                    "track": "headers",
                    "text": text,
                    "expected": expected,
                    "must_review": scenario not in ["standard", "formatted"],
                }
            )
    (ROOT / "invoices.json").write_text(json.dumps(cases, indent=2) + "\n")


if __name__ == "__main__":
    main()
