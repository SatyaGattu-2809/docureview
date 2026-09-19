"""Generate synthetic raster fixtures; Pillow is needed only to regenerate them."""

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent


def main():
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 34)
    cases = []
    for name, angle in [("scan_clear", 0), ("scan_skew", 2)]:
        text = (
            "Invoice number: OCR-201\nVendor: Ember Research\nInvoice date: 2026-09-01\n"
            "Currency: USD\nSubtotal: 1000.00\nTax: 80.00\nTotal: 1080.00\n"
        )
        image = Image.new("RGB", (1500, 650), "white")
        ImageDraw.Draw(image).multiline_text((70, 70), text, font=font, fill="black", spacing=15)
        if angle:
            image = image.rotate(angle, fillcolor="white")
        image.save(ROOT / "ocr" / f"{name}.png")
        cases.append(
            {
                "id": name,
                "path": f"ocr/{name}.png",
                "text": text,
                "expected": {
                    "invoice_number": "OCR-201",
                    "vendor": "Ember Research",
                    "invoice_date": "2026-09-01",
                    "currency": "USD",
                    "subtotal": "1000.00",
                    "tax": "80.00",
                    "total": "1080.00",
                },
            }
        )
    (ROOT / "ocr/labels.json").write_text(json.dumps(cases, indent=2) + "\n")


if __name__ == "__main__":
    main()
