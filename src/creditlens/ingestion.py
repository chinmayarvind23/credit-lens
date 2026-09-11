"""Born-digital extraction fails explicitly when page evidence cannot be recovered."""

from collections.abc import Sequence
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, __version__
from pypdf.errors import PdfReadError

from creditlens.domain import Page


class ExtractionError(ValueError):
    """Callers must retain a failed job instead of silently indexing invented source text."""


def text_hash(text: str) -> str:
    """Hash exact canonical text so changing extracted evidence invalidates its identity."""
    return sha256(text.encode("utf-8")).hexdigest()


def _validate_metadata(pages: Sequence[Page]) -> None:
    """Reject mixed document scopes before attaching trusted metadata to extracted text."""
    if not pages or [page.page for page in pages] != list(range(1, len(pages) + 1)):
        raise ExtractionError("Metadata must contain contiguous one-based pages")
    identities = {
        (page.tenant_id, page.borrower_id, page.document_id, page.document_version)
        for page in pages
    }
    if len(identities) != 1:
        raise ExtractionError("One PDF must have one document identity and tenant scope")


def ingest_pdf(
    path: Path, metadata: Sequence[Page], max_bytes: int = 25_000_000
) -> tuple[Page, ...]:
    """Extract real PDF text; metadata supplies provenance but never substitutes source content."""
    _validate_metadata(metadata)
    if not 1 <= max_bytes <= 25_000_000:
        raise ExtractionError("PDF exceeds configured input size limit")
    try:
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
        return ingest_pdf_bytes(data, metadata, max_bytes=max_bytes)
    except OSError as error:
        raise ExtractionError("PDF could not be read") from error


def ingest_pdf_bytes(
    data: bytes, metadata: Sequence[Page], max_bytes: int = 25_000_000
) -> tuple[Page, ...]:
    """Parse verified bytes, preventing source replacement between hashing and extraction."""
    _validate_metadata(metadata)
    try:
        if not 1 <= max_bytes <= 25_000_000 or len(data) > max_bytes:
            raise ExtractionError("PDF exceeds configured input size limit")
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise ExtractionError("Encrypted documents require an explicit decryption workflow")
        if len(reader.pages) != len(metadata):
            raise ExtractionError("Physical PDF page count does not match the manifest")
        result = []
        for source, page in zip(reader.pages, metadata, strict=True):
            contents = source.get_contents()
            if contents is not None and len(contents.get_data()) > max_bytes:
                raise ExtractionError("Expanded page stream exceeds configured size limit")
            text = (source.extract_text() or "").strip()
            if not text:
                raise ExtractionError(f"Page {page.page} requires OCR or has no readable evidence")
            result.append(
                page.model_copy(
                    update={
                        "text": text,
                        "content_hash": text_hash(text),
                        "parser_version": f"pypdf-{__version__}",
                        "extraction_confidence": 1,
                    }
                )
            )
        return tuple(result)
    except (PdfReadError, OSError) as error:
        raise ExtractionError("PDF could not be read") from error
