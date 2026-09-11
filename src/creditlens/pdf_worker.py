"""No-network parser entry point: bounded source and manifest in, validated JSON out."""

import argparse
import json
import logging
from hashlib import sha256
from pathlib import Path

from pydantic import Field, model_validator

from creditlens.domain import Page, StrictModel
from creditlens.ingestion import ExtractionError, ingest_pdf_bytes
from creditlens.ingestion_jobs import IngestionInput


class ParsedDocument(StrictModel):
    """Only source-bound, bounded page output may cross the parser process boundary."""

    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    pages: tuple[Page, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def bounded_output(self) -> "ParsedDocument":
        """Limit serialized output before writing it to the parent process or private artifact."""
        if len(self.model_dump_json().encode()) > 8_388_608:
            raise ValueError("Parser output exceeds the 8 MiB limit")
        return self


def extract_document(path: Path, source: IngestionInput) -> ParsedDocument:
    """Verify the submitted immutable source before parsing, independent of metadata text."""
    if source.parser != "digital":
        raise ExtractionError("The PDF text worker does not perform OCR")
    with path.open("rb") as stream:
        data = stream.read(25_000_001)
    if len(data) > 25_000_000 or sha256(data).hexdigest() != source.source_sha256:
        raise ExtractionError("PDF source does not match the bounded submitted hash")
    return ParsedDocument(
        source_sha256=source.source_sha256, pages=ingest_pdf_bytes(data, source.pages)
    )


def main() -> None:
    """Return structured success or curated failure; parser exceptions may contain source text."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/input/source.pdf"))
    parser.add_argument("--manifest", type=Path, default=Path("/input/manifest.json"))
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        with args.manifest.open("rb") as stream:
            raw = stream.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("Manifest exceeds limit")
        source = IngestionInput.model_validate_json(raw)
        print(extract_document(args.source, source).model_dump_json())
    except Exception:
        # This final process boundary deliberately hides arbitrary parser exception strings.
        print(json.dumps({"error_code": "extraction_failed"}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
