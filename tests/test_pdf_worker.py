"""Real PDF extraction tests verify the isolated worker's success and failure contract."""

import json
import logging
import sys
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from creditlens.corpus import _write_pdf, borrower_pages
from creditlens.ingestion import ExtractionError
from creditlens.ingestion_jobs import IngestionInput
from creditlens.pdf_worker import ParsedDocument, extract_document, main


def test_worker_extracts_actual_pdf_bytes_and_restores_digital_confidence() -> None:
    """Sanitized metadata starts at zero confidence; successful text parsing sets its own status."""
    with TemporaryDirectory(prefix="creditlens-pdf-worker-") as directory:
        path = Path(directory) / "source.pdf"
        pages = borrower_pages(1)[:2]
        _write_pdf(path, pages)
        source = IngestionInput(
            source_sha256=sha256(path.read_bytes()).hexdigest(), pages=pages, parser="digital"
        ).sanitized()
        result = extract_document(path, source)
        assert result.source_sha256 == source.source_sha256
        assert len(result.pages) == 2
        assert "operating_cash_flow=180000.00" in result.pages[1].text
        assert all(page.extraction_confidence == 1 for page in result.pages)
        assert all("pending extraction" not in page.text for page in result.pages)
        assert all(page.parser_version.startswith("pypdf-") for page in result.pages)


def test_worker_rejects_changed_source_wrong_parser_and_malformed_pdf() -> None:
    """A valid manifest cannot turn altered or unreadable source bytes into evidence."""
    with TemporaryDirectory(prefix="creditlens-pdf-worker-") as directory:
        path = Path(directory) / "source.pdf"
        path.write_bytes(b"%PDF-1.7\nnot structurally valid")
        source = IngestionInput(
            source_sha256=sha256(path.read_bytes()).hexdigest(),
            pages=borrower_pages(1)[:1],
            parser="digital",
        ).sanitized()
        with pytest.raises(ExtractionError):
            extract_document(path, source.model_copy(update={"source_sha256": "b" * 64}))
        with pytest.raises(ExtractionError):
            extract_document(path, source.model_copy(update={"parser": "ocr"}))
        with pytest.raises(ExtractionError):
            extract_document(path, source)


def test_worker_cli_uses_bounded_manifest_and_curated_failures(monkeypatch, capsys) -> None:
    """Exercise the real entry point without allowing malformed source text into failure output."""
    previous_logging = logging.root.manager.disable
    try:
        with TemporaryDirectory(prefix="creditlens-pdf-cli-") as directory:
            root = Path(directory)
            source_path, manifest_path = root / "source.pdf", root / "manifest.json"
            pages = borrower_pages(1)[:1]
            _write_pdf(source_path, pages)
            source = IngestionInput(
                source_sha256=sha256(source_path.read_bytes()).hexdigest(),
                pages=pages,
                parser="digital",
            ).sanitized()
            manifest_path.write_text(source.model_dump_json(), encoding="utf-8")
            monkeypatch.setattr(
                sys,
                "argv",
                ["pdf_worker", "--source", str(source_path), "--manifest", str(manifest_path)],
            )
            main()
            assert json.loads(capsys.readouterr().out)["source_sha256"] == source.source_sha256
            for content in ("x" * 1_000_001, '{"private":"do not expose this"}'):
                manifest_path.write_text(content, encoding="utf-8")
                with pytest.raises(SystemExit) as error:
                    main()
                assert error.value.code == 2
                assert json.loads(capsys.readouterr().out) == {"error_code": "extraction_failed"}
    finally:
        logging.disable(previous_logging)


def test_parser_output_and_source_size_are_independently_bounded() -> None:
    """A valid hash cannot bypass byte limits, and huge text cannot overflow worker output."""
    with pytest.raises(ValueError, match="8 MiB"):
        ParsedDocument(
            source_sha256="a" * 64,
            pages=(borrower_pages(1)[0].model_copy(update={"text": "x" * 8_388_609}),),
        )
    with TemporaryDirectory(prefix="creditlens-pdf-limit-") as directory:
        source_path = Path(directory) / "oversize.pdf"
        data = b"%PDF-" + b"x" * 25_000_000
        source_path.write_bytes(data)
        source = IngestionInput(
            source_sha256=sha256(data).hexdigest(), pages=borrower_pages(1)[:1], parser="digital"
        )
        with pytest.raises(ExtractionError, match="bounded"):
            extract_document(source_path, source)
