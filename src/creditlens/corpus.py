"""Authored synthetic lending fixtures expose scale and template limits explicitly."""

import json
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from textwrap import wrap
from typing import Any

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen.canvas import Canvas

from creditlens.domain import Borrower, Page
from creditlens.ingestion import ingest_pdf, text_hash

CORPUS_VERSION = "synthetic-lending-v1"
INDUSTRIES = ("Manufacturing", "Healthcare", "Logistics", "Retail", "Professional services")
PAGE_SECTIONS = (
    "application",
    "financial_summary",
    "balance_sheet",
    "cash_flow",
    "debt_schedule",
    "collateral",
    "bank_january",
    "bank_february",
    "bank_march",
    "bank_april",
    "bank_may",
    "bank_june",
    "ownership",
    "tax_summary",
    "receivables",
    "covenant",
    "analyst_memo",
    "restricted_review",
)
POLICY_TOPICS = (
    ("dscr", "Debt service coverage", "1.25", "ratio"),
    ("ltv", "Loan to value", "0.75", "ratio"),
    ("liquidity", "Current ratio", "1.20", "ratio"),
    ("leverage", "Debt to assets", "0.65", "ratio"),
    ("bank_history", "Bank statement history", "6", "months"),
    ("financial_age", "Financial statement age", "180", "days"),
    ("concentration", "Customer concentration", "0.30", "ratio"),
    ("receivables_age", "Receivables eligibility age", "90", "days"),
    ("ownership", "Beneficial ownership disclosure", "0.25", "ratio"),
    ("review_cycle", "Annual credit review", "12", "months"),
)
POLICY_ASPECTS = (
    "threshold",
    "evidence",
    "exception",
    "currency",
    "period",
    "treatment",
    "escalation",
    "review",
)


def borrower_fixture(number: int) -> Borrower:
    """Stable borrower identifiers keep authored qrels independent of generated chunk IDs."""
    names = {
        1: "Northstar Fabrication",
        2: "Cedar Freight",
        3: "Harbor Medical",
        4: "Prairie Foods",
    }
    return Borrower(
        borrower_id=f"borrower-{number:03}",
        name=names.get(number, f"Synthetic Enterprise {number:03}"),
        industry=INDUSTRIES[(number - 1) % len(INDUSTRIES)],
    )


def build_demo_borrowers() -> tuple[Borrower, ...]:
    """Expose five demo borrowers without granting access to the rest of the corpus."""
    return tuple(borrower_fixture(number) for number in range(1, 6))


def _page(text: str, **metadata: Any) -> Page:
    """Centralize source metadata so corpus fixtures obey the same contract as extraction."""
    return Page(
        text=text, content_hash=text_hash(text), parser_version="authored-source-v1", **metadata
    )


def _finance(number: int) -> dict[str, str]:
    """Author exact decimal inputs; deliberately omit borrower three's debt service evidence."""
    cash_flow = Decimal(180000 + (number - 1) * 3700)
    debt_service = Decimal(120000 + (number - 1) * 1900)
    if number == 2:
        cash_flow, debt_service = Decimal(132000), Decimal(120000)
    result = {
        "operating_cash_flow": f"{cash_flow:.2f}",
        "annual_debt_service": f"{debt_service:.2f}",
        "total_debt": f"{800000 + number * 1100:.2f}",
        "total_assets": f"{1600000 + number * 5200:.2f}",
        "annual_revenue": f"{2400000 + number * 18000:.2f}",
        "currency": "USD",
        "period": "2025",
    }
    if number == 3:
        del result["annual_debt_service"]
    return result


def _borrower_content(number: int, section: str) -> str:
    """Vary facts and document purposes while retaining auditable synthetic templates."""
    borrower = borrower_fixture(number)
    facts = _finance(number)
    opening = f"Borrower: {borrower.name}. Borrower ID: {borrower.borrower_id}.\n"
    if section == "financial_summary":
        return (
            opening
            + "Financial inputs for the 2025 annual review.\n"
            + "\n".join(f"{key}={value}" for key, value in facts.items())
        )
    if section.startswith("bank_"):
        month = section.removeprefix("bank_")
        return opening + (
            f"{month.title()} 2025 bank statement. Account ending {1000 + number}.\n"
            f"Deposits: USD {210000 + number * 1173}. "
            f"Withdrawals: USD {170000 + number * 891}.\n"
            "Statement reconciliation remains subject to analyst review."
        )
    content = {
        "application": f"Industry: {borrower.industry}. "
        f"Requested loan: USD {500000 + number * 500}. "
        "Purpose: equipment renewal. Final credit decision requires human review.",
        "balance_sheet": f"Total assets: USD {facts['total_assets']}. "
        f"Total debt: USD {facts['total_debt']}. Period ended 2025-12-31.",
        "cash_flow": f"Operating cash flow: USD {facts['operating_cash_flow']}. "
        "Prepared on a cash basis for the twelve months ended 2025-12-31.",
        "debt_schedule": (
            "Annual debt service schedule is missing; request the signed schedule."
            if number == 3
            else "Annual debt service: USD "
            + facts.get("annual_debt_service", "")
            + ". Includes principal and interest."
        ),
        "collateral": f"Equipment valuation: USD {900000 + number * 2300}. "
        "Independent appraisal dated 2026-01-15. Existing liens require verification.",
        "ownership": f"Owner A holds {60 + number % 20}% and Owner B holds {40 - number % 20}%. "
        "Both owners are disclosed in the synthetic ownership register.",
        "tax_summary": f"Reported annual revenue: USD {facts['annual_revenue']}. "
        "Tax reconciliation is prepared for calendar year 2025.",
        "receivables": f"Eligible receivables: USD {320000 + number * 910}. "
        f"Over-90-day receivables: USD {12000 + number * 40}. "
        "Aging report uses invoice due dates.",
        "covenant": "DSCR is evaluated against current lender policy using verified annual inputs. "
        "A threshold failure requires an exception review, not an automatic loan decision.",
        "analyst_memo": (
            "Conflicting evidence: signed correction reports operating_cash_flow=90000.00 "
            "for period=2025. Reconcile with the financial summary before disposition."
            if number == 4
            else "Analyst memo: "
            + (
                "Missing documents: signed annual debt service schedule."
                if number == 3
                else "No material financial discrepancy was recorded in this fixture."
            )
        ),
        "restricted_review": "Credit-officer-only review. Internal watchlist code: "
        f"RESTRICTED-{number:03}. This source must never enter underwriter context.",
    }
    return opening + content[section]


def borrower_pages(number: int) -> tuple[Page, ...]:
    """Keep every template tied to a physical package page and explicit document ACL."""
    borrower = borrower_fixture(number)
    return tuple(
        _page(
            _borrower_content(number, section),
            tenant_id="demo-bank" if number <= 150 else "other-bank",
            borrower_id=borrower.borrower_id,
            document_id=f"{borrower.borrower_id}-package",
            document_version="v1",
            page=index,
            document_kind=section,
            title=f"{borrower.name} / {section.replace('_', ' ')}",
            section=section,
            acl_groups=("credit-officer",) if index == 18 else ("underwriting",),
            valid_from=date(2026, 1, 1),
        )
        for index, section in enumerate(PAGE_SECTIONS, start=1)
    )


def _policy_text(topic: tuple[str, str, str, str], aspect: str, version: int) -> str:
    """Author distinct policy conditions and explicit version changes rather than page padding."""
    key, label, threshold, unit = topic
    if key == "dscr":
        threshold = {1: "1.20", 2: "1.25", 3: "1.30"}[version]
    rules = {
        "threshold": f"The {label.lower()} policy threshold is {threshold} {unit}. "
        + (
            "DSCR equals operating cash flow divided by annual debt service. "
            "Minimum DSCR is required; lower ratios require an exception review."
            if key == "dscr"
            else "Apply the threshold to verified supporting evidence."
        ),
        "evidence": f"For {label.lower()}, collect the signed source schedule "
        "and its reporting date. "
        "If the required source is missing, use INSUFFICIENT_EVIDENCE and request it.",
        "exception": f"A {label.lower()} exception requires a written rationale, "
        "compensating factors "
        "and a credit officer review. The assistant cannot grant the exception.",
        "currency": f"For {label.lower()}, financial amounts must share a reporting currency. "
        "Document the exchange rate and valuation date when translating source amounts.",
        "period": f"The {label.lower()} review must align reporting periods "
        "before comparing inputs. "
        "Do not combine a quarterly numerator with an annual denominator.",
        "treatment": f"For {label.lower()}, unresolved conflicting values "
        "require MATERIAL_CONFLICT. "
        "Keep both source pages and request reconciliation instead of averaging values.",
        "escalation": f"Escalate ambiguous {label.lower()} evidence "
        "to the assigned credit officer. "
        "Record HUMAN_JUDGMENT_REQUIRED when the policy leaves material discretion.",
        "review": f"The analyst must record the {label.lower()} policy version and effective date. "
        "Future-effective rules are excluded until their start date; expired rules are excluded.",
    }
    return f"Policy topic: {label}. Section: {key}.{aspect}.\n{rules[aspect]}"


def policy_pages() -> tuple[Page, ...]:
    """Half-open policy windows provide current, historical and future-effective evidence."""
    windows = (
        (date(2025, 1, 1), date(2026, 1, 1)),
        (date(2026, 1, 1), date(2027, 1, 1)),
        (date(2027, 1, 1), None),
    )
    result = []
    for version, (start, end) in enumerate(windows, start=1):
        for topic_index, topic in enumerate(POLICY_TOPICS):
            for aspect_index, aspect in enumerate(POLICY_ASPECTS):
                result.append(
                    _page(
                        _policy_text(topic, aspect, version),
                        tenant_id="demo-bank",
                        borrower_id=None,
                        document_id="lending-policy",
                        document_version=f"v{version}",
                        page=topic_index * 8 + aspect_index + 1,
                        document_kind="policy",
                        title=f"Lending policy / {topic[1]}",
                        section=f"{topic[0]}.{aspect}",
                        acl_groups=("underwriting",),
                        valid_from=start,
                        valid_to=end,
                    )
                )
    return tuple(result)


def build_demo_pages() -> tuple[Page, ...]:
    """Small in-memory fixtures serve offline tests; physical extraction is measured separately."""
    return policy_pages() + tuple(page for number in range(1, 6) for page in borrower_pages(number))


def _write_pdf(path: Path, pages: Sequence[Page]) -> None:
    """Render fixed page boundaries with deterministic PDF metadata and readable line wrapping."""
    canvas = Canvas(str(path), pagesize=letter, invariant=1, pageCompression=1)
    for page in pages:
        canvas.setFillColorRGB(0.07, 0.17, 0.23)
        canvas.rect(0, 716, 612, 76, fill=1, stroke=0)
        canvas.setFillColorRGB(1, 1, 1)
        canvas.setFont("Helvetica-Bold", 16)
        canvas.drawString(44, 753, "CreditLens synthetic lending evidence")
        canvas.setFont("Helvetica", 10)
        canvas.drawString(44, 732, f"{page.document_id} / {page.document_version}")
        canvas.setFillColorRGB(0.10, 0.14, 0.16)
        body = canvas.beginText(44, 680)
        body.setFont("Helvetica", 11)
        body.setLeading(17)
        for paragraph in (page.title + "\n\n" + page.text).splitlines():
            for line in wrap(paragraph, width=84) or [""]:
                body.textLine(line)
        canvas.drawText(body)
        canvas.setFont("Helvetica", 9)
        canvas.drawString(
            44, 48, "Synthetic fixture. No real borrower data. Human credit review required."
        )
        canvas.drawRightString(568, 32, f"Page {page.page} / {len(pages)}")
        canvas.showPage()
    canvas.save()


def build_corpus(output: Path, borrower_count: int = 200) -> dict[str, Any]:
    """Emit physical files, parse them back, and preserve count and duplicate evidence."""
    if not 4 <= borrower_count <= 200:
        raise ValueError("borrower_count must be between 4 and 200")
    output.mkdir(parents=True, exist_ok=True)
    pages = policy_pages() + tuple(
        page for number in range(1, borrower_count + 1) for page in borrower_pages(number)
    )
    documents: dict[tuple[str, str], list[Page]] = defaultdict(list)
    for page in pages:
        documents[(page.document_id, page.document_version)].append(page)
    extracted: list[Page] = []
    manifest = []
    for (document_id, version), document_pages in sorted(documents.items()):
        path = output / f"{document_id}--{version}.pdf"
        _write_pdf(path, document_pages)
        extracted.extend(ingest_pdf(path, document_pages))
        manifest.append(
            {
                "document_id": document_id,
                "version": version,
                "path": path.name,
                "pages": len(document_pages),
                "sha256": sha256(path.read_bytes()).hexdigest(),
            }
        )
    (output / "pages.jsonl").write_text(
        "\n".join(page.model_dump_json() for page in extracted) + "\n", encoding="utf-8"
    )
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    hashes = Counter(page.content_hash for page in pages)
    report = {
        "corpus_version": CORPUS_VERSION,
        "physical_pages": len(extracted),
        "documents": len(documents),
        "borrowers": borrower_count,
        "policy_topic_aspect_templates": 80,
        "borrower_page_templates": 18,
        "distinct_source_texts": len(hashes),
        "exact_duplicate_source_pages": sum(count - 1 for count in hashes.values()),
        "tenant_page_counts": dict(Counter(page.tenant_id for page in extracted)),
        "restricted_pages": sum(page.acl_groups == ("credit-officer",) for page in extracted),
        "parser": extracted[0].parser_version,
        "limitation": "Authored synthetic templates; page count is scale, not external validity.",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
