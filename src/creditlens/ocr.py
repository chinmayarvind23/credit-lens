"""Normalize offline OCR as review evidence without promoting uncertain text into retrieval."""

import json
from hashlib import sha256
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from creditlens.domain import Page, StrictModel
from creditlens.ingestion import ExtractionError


class OcrBlock(StrictModel):
    """Keep table markup and reading order verbatim; layout scores are not text confidence."""

    label: str = Field(min_length=1, max_length=80)
    text: str = Field(max_length=100_000)
    bbox: tuple[float, float, float, float]
    order: int | None = Field(default=None, ge=1)


class ScannedPage(StrictModel):
    """Bind a review artifact to physical source bytes and trusted document scope."""

    metadata: Page
    pdf_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    image_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    raw_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    models_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    parser: Literal["paddleocr-3.7.0-vl-1.6"] = "paddleocr-3.7.0-vl-1.6"
    width: int = Field(gt=0, le=4000)
    height: int = Field(gt=0, le=4000)
    blocks: tuple[OcrBlock, ...] = Field(min_length=1, max_length=256)
    status: Literal["REVIEW_REQUIRED"] = "REVIEW_REQUIRED"
    recognition_confidence: None = None

    @model_validator(mode="after")
    def bounded_layout(self) -> "ScannedPage":
        """Reject corrupt geometry and ambiguous reading order before showing an artifact."""
        if self.width * self.height > 4_000_000:
            raise ValueError("OCR image exceeds pixel limit")
        orders = [block.order for block in self.blocks if block.order is not None]
        if orders != list(range(1, len(orders) + 1)):
            raise ValueError("OCR reading order must be contiguous and unique")
        for block in self.blocks:
            left, top, right, bottom = block.bbox
            if not (0 <= left < right <= self.width and 0 <= top < bottom <= self.height):
                raise ValueError("OCR bounding box exceeds physical image")
        if not any(block.text.strip() for block in self.blocks):
            raise ValueError("OCR page contains no readable evidence")
        if self.metadata.extraction_confidence != 0:
            raise ValueError("Unreviewed OCR metadata must remain excluded from retrieval")
        return self


def normalize_vl(
    raw: bytes,
    metadata: Page,
    *,
    pdf_sha256: str,
    image_sha256: str,
    models_sha256: str,
    generation_complete: bool,
) -> ScannedPage:
    """Accept bounded worker output only; the trusted renderer supplies physical-page hashes."""
    if not generation_complete:
        raise ExtractionError("OCR generation completion is unverified or truncated")
    if len(raw) > 1_000_000:
        raise ExtractionError("OCR output exceeds size limit")
    try:
        payload = json.loads(raw)
        blocks = tuple(
            OcrBlock(
                label=block["block_label"],
                text=block["block_content"],
                bbox=block["block_bbox"],
                order=block["block_order"],
            )
            for block in payload["parsing_res_list"]
        )
        # Source metadata text is discarded even when extraction fails or returns an empty page.
        text = "\n\n".join(block.text for block in blocks if block.text.strip())
        page = Page.model_validate(
            metadata.model_dump()
            | {
                "text": text,
                "content_hash": sha256(text.encode("utf-8")).hexdigest(),
                "parser_version": "paddleocr-3.7.0-vl-1.6-unreviewed",
                "extraction_confidence": 0,
            }
        )
        return ScannedPage(
            metadata=page,
            pdf_sha256=pdf_sha256,
            image_sha256=image_sha256,
            raw_sha256=sha256(raw).hexdigest(),
            models_sha256=models_sha256,
            width=payload["width"],
            height=payload["height"],
            blocks=blocks,
        )
    except (ValueError, TypeError, KeyError, ValidationError) as error:
        raise ExtractionError("OCR output is malformed or has no readable evidence") from error


class OcrDocument(StrictModel):
    """Keep a bounded, immutable review snapshot with the original extraction provenance."""

    pages: tuple[ScannedPage, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def consistent_text(self) -> "OcrDocument":
        """Reject forged metadata text rather than showing blocks that differ from admission."""
        if len(self.model_dump_json().encode("utf-8")) > 8_000_000:
            raise ValueError("OCR document exceeds review size limit")
        for page in self.pages:
            text = "\n\n".join(block.text for block in page.blocks if block.text.strip())
            if (
                page.metadata.text != text
                or page.metadata.content_hash != sha256(text.encode("utf-8")).hexdigest()
            ):
                raise ValueError("OCR metadata must match the extracted blocks")
        return self

    def digest(self) -> str:
        """Hash canonical validated JSON so a decision cannot target a different snapshot."""
        return sha256(self.model_dump_json().encode("utf-8")).hexdigest()


class OcrReview(StrictModel):
    """An explicit human decision may correct text but cannot alter source scope."""

    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=2000)
    corrected_text: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def bounded_corrections(self) -> "OcrReview":
        """Require readable full-page replacements and keep operator submissions bounded."""
        if not self.reason.strip() or len(self.model_dump_json().encode()) > 8_000_000:
            raise ValueError("Invalid review reason or size")
        if self.corrected_text is not None:
            if self.decision != "approve" or any(
                not text.strip() or len(text) > 100_000 for text in self.corrected_text
            ):
                raise ValueError("Corrections require approval and readable bounded text")
        return self


class OcrReviewSnapshot(StrictModel):
    """Return the canonical decision hash alongside the exact content the reviewer sees."""

    artifact_sha256: str
    artifact: OcrDocument
