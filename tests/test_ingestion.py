"""Physical-PDF tests verify page identity, provenance and extraction failure boundaries."""

import json
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from pypdf import PdfWriter

from creditlens.corpus import (
    _write_pdf,
    borrower_pages,
    build_corpus,
    build_demo_pages,
    policy_pages,
)
from creditlens.ingestion import ExtractionError, ingest_pdf, text_hash


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """Avoid pytest's Windows symlink cleanup failure using an isolated temporary directory."""
    with TemporaryDirectory(prefix="creditlens-ingestion-") as directory:
        yield Path(directory)


def test_pdf_extraction_preserves_physical_pages_and_uses_actual_text(tmp_path: Path) -> None:
    """Changing source metadata text must not turn it into fabricated extracted evidence."""
    pages = borrower_pages(1)
    path = tmp_path / "borrower.pdf"
    _write_pdf(path, pages)
    metadata = tuple(page.model_copy(update={"text": "THIS IS NOT PDF CONTENT"}) for page in pages)
    extracted = ingest_pdf(path, metadata)
    assert len(extracted) == 18
    assert [page.page for page in extracted] == list(range(1, 19))
    assert "operating_cash_flow=180000.00" in extracted[1].text
    assert "THIS IS NOT PDF CONTENT" not in extracted[1].text
    assert extracted[1].content_hash == text_hash(extracted[1].text)
    assert extracted[1].parser_version.startswith("pypdf-")
    assert extracted[-1].acl_groups == ("credit-officer",)


def test_parser_rejects_blank_count_mismatch_and_mixed_scope(tmp_path: Path) -> None:
    """Unavailable evidence and invalid manifests cannot silently enter the retrieval index."""
    path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(path)
    pages = borrower_pages(1)
    with pytest.raises(ExtractionError, match="requires OCR"):
        ingest_pdf(path, pages[:1])
    with pytest.raises(ExtractionError, match="page count"):
        ingest_pdf(path, pages)
    with pytest.raises(ExtractionError, match="identity"):
        ingest_pdf(path, (pages[0], pages[1].model_copy(update={"tenant_id": "other"})))
    with pytest.raises(ExtractionError, match="contiguous"):
        ingest_pdf(path, pages[1:2])
    with pytest.raises(ExtractionError, match="size limit"):
        ingest_pdf(path, pages[:1], max_bytes=1)


def test_malformed_pdf_is_explicit_failure(tmp_path: Path) -> None:
    """Bad input produces a typed extraction error rather than indexing a partial document."""
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"not a PDF")
    with pytest.raises(ExtractionError, match="could not be read"):
        ingest_pdf(path, borrower_pages(1)[:1])


def test_demo_has_distinct_scenarios_and_nonoverlapping_policy_versions() -> None:
    """Demo fixtures preserve missing, exception and conflict cases as actual source evidence."""
    assert len(build_demo_pages()) == 330
    assert "annual_debt_service=" not in borrower_pages(3)[1].text
    assert "132000.00" in borrower_pages(2)[1].text
    assert "Conflicting evidence" in borrower_pages(4)[16].text
    assert borrower_pages(151)[0].tenant_id == "other-bank"
    dscr = [page for page in policy_pages() if page.page == 1]
    assert [page.document_version for page in dscr] == ["v1", "v2", "v3"]
    assert dscr[0].valid_to == dscr[1].valid_from
    assert dscr[1].valid_to == dscr[2].valid_from


def test_corpus_rerun_produces_identical_pdf_hashes(tmp_path: Path) -> None:
    """A repeated small generation validates byte determinism without a redundant 3840-page test."""
    first = build_corpus(tmp_path / "first", borrower_count=4)
    second = build_corpus(tmp_path / "second", borrower_count=4)
    assert first == second
    assert first["physical_pages"] == 312
    assert first["documents"] == 7
    assert first["exact_duplicate_source_pages"] > 0
    manifest = json.loads((tmp_path / "first" / "manifest.json").read_text())
    other = json.loads((tmp_path / "second" / "manifest.json").read_text())
    assert manifest == other


@pytest.mark.parametrize("count", [0, 3, 201])
def test_invalid_corpus_size_is_rejected(tmp_path: Path, count: int) -> None:
    """Bound generated artifacts so a mistaken size cannot consume arbitrary local resources."""
    with pytest.raises(ValueError, match="borrower_count"):
        build_corpus(tmp_path, borrower_count=count)
