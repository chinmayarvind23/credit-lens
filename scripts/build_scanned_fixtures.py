"""Create image-only synthetic PDFs with separate source annotations for actual OCR evaluation."""

import argparse
import json
from hashlib import sha256
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pypdf import PdfReader
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

SIZE = (1240, 1754)


def label(
    draw: ImageDraw.ImageDraw, xy: tuple[int, int], value: str, font: ImageFont.FreeTypeFont
) -> None:
    """Draw exact annotation text as pixels; the PDF will contain no selectable text layer."""
    draw.text(xy, value, fill="#172724", font=font)


def table_page(font_path: Path) -> tuple[Image.Image, dict[str, object]]:
    """Use explicit rows and years to expose OCR cell associations and decimal errors."""
    picture = Image.new("RGB", SIZE, "white")
    draw = ImageDraw.Draw(picture)
    heading = ImageFont.truetype(str(font_path), 38)
    body = ImageFont.truetype(str(font_path), 27)
    label(draw, (85, 100), "CREDITLENS SYNTHETIC FINANCIAL STATEMENT", heading)
    label(draw, (85, 175), "Borrower: Northstar Fabrication | Currency: USD", body)
    label(draw, (85, 225), "Annual reporting periods ending December 31", body)
    rows = [
        ["Metric", "FY2024", "FY2025"],
        ["Operating cash flow", "168000.00", "180000.00"],
        ["Annual debt service", "112000.00", "120000.00"],
        ["Total debt", "780000.00", "801100.00"],
        ["Total assets", "1500000.00", "1605200.00"],
    ]
    for index, row in enumerate(rows):
        y = 355 + index * 95
        for x, value in zip((110, 665, 940), row, strict=True):
            label(draw, (x, y), value, body)
        draw.line((85, y + 65, 1160, y + 65), fill="#85938c", width=2)
    for x in (85, 625, 905, 1160):
        draw.line((x, 330, x, 800), fill="#85938c", width=2)
    label(draw, (85, 940), "DSCR = operating cash flow / annual debt service.", body)
    label(draw, (85, 995), "This fictional statement supports software testing only.", body)
    label(draw, (85, 1600), "Document: scan-financial-v1 | Physical page: 1", body)
    return picture, {
        "table": rows,
        "required_text": ["Northstar Fabrication", "Currency: USD", "DSCR"],
    }


def columns_page(font_path: Path) -> tuple[Image.Image, dict[str, object]]:
    """Separate two policy columns so layout order can be checked independently of word recovery."""
    picture = Image.new("RGB", SIZE, "white")
    draw = ImageDraw.Draw(picture)
    heading = ImageFont.truetype(str(font_path), 40)
    body = ImageFont.truetype(str(font_path), 28)
    label(draw, (85, 100), "SYNTHETIC LENDING POLICY", heading)
    label(draw, (85, 180), "Version v2 | Effective date: 2026-01-01", body)
    columns = [
        [
            "Debt service coverage",
            "Minimum DSCR is 1.25.",
            "Use annual operating cash flow",
            "divided by annual debt service.",
            "Reporting periods must agree.",
            "Currencies must agree.",
            "Missing inputs require review.",
        ],
        [
            "Exception review",
            "A lower ratio requires an exception.",
            "Record a written rationale.",
            "Document compensating factors.",
            "Request credit officer review.",
            "The assistant cannot approve",
            "a loan or grant an exception.",
        ],
    ]
    for x, lines in zip((85, 650), columns, strict=True):
        for index, line in enumerate(lines):
            label(draw, (x, 330 + index * 65), line, body)
    draw.line((605, 320, 605, 855), fill="#85938c", width=2)
    label(draw, (85, 1600), "Document: scan-policy-v2 | Physical page: 1", body)
    return picture, {"columns": columns, "required_text": ["1.25", "2026-01-01", "credit officer"]}


def write_fixture(
    output: Path, name: str, picture: Image.Image, expected: dict[str, object]
) -> dict:
    """Bind annotations to the actual image-only PDF and bitmap hashes without hidden answers."""
    png, pdf = output / f"{name}.png", output / f"{name}.pdf"
    picture.save(png)
    canvas = Canvas(str(pdf), pagesize=(620, 877), invariant=1)
    canvas.drawImage(ImageReader(picture), 0, 0, width=620, height=877)
    canvas.showPage()
    canvas.save()
    reader = PdfReader(pdf, strict=True)
    if len(reader.pages) != 1 or (reader.pages[0].extract_text() or "").strip():
        raise ValueError("The OCR fixture must contain one image-only physical page")
    return {
        "name": name,
        "pdf": pdf.name,
        "image": png.name,
        "page": 1,
        "pdf_sha256": sha256(pdf.read_bytes()).hexdigest(),
        "image_sha256": sha256(png.read_bytes()).hexdigest(),
        "width": picture.width,
        "height": picture.height,
        "expected": expected,
    }


def main() -> None:
    """Write a new evidence directory so revised fixtures cannot overwrite a measured baseline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Use a new fixture directory")
    args.output.mkdir(parents=True)
    financial, table = table_page(args.font)
    policy, columns = columns_page(args.font)
    degraded = financial.resize((620, 877)).filter(ImageFilter.GaussianBlur(1.2)).resize(SIZE)
    degraded = degraded.rotate(4, fillcolor="white")
    records = [
        write_fixture(args.output, "financial-table", financial, table),
        write_fixture(args.output, "policy-columns", policy, columns),
        write_fixture(args.output, "degraded-table", degraded, table | {"degraded": True}),
        write_fixture(
            args.output,
            "blank-page",
            Image.new("RGB", SIZE, "white"),
            {"must_fail": "no readable evidence"},
        ),
    ]
    manifest = {
        "schema_version": 1,
        "synthetic": True,
        "font_sha256": sha256(args.font.read_bytes()).hexdigest(),
        "fixtures": records,
    }
    (args.output / "annotations.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"output": str(args.output), "image_only_pdfs": len(records)}))


if __name__ == "__main__":
    main()
