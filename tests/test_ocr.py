"""Adversarial OCR output checks protect source identity and the retrieval confidence gate."""

import json

import pytest

from creditlens.corpus import borrower_pages
from creditlens.ingestion import ExtractionError, text_hash
from creditlens.ocr import normalize_vl


def output() -> dict:
    """Use a small vendor-shaped contract fixture, not a claimed model-quality evaluation."""
    return {
        "width": 1240,
        "height": 1754,
        "parsing_res_list": [
            {
                "block_label": "table",
                "block_content": "<table><tr><td>180000.00</td></tr></table>",
                "block_bbox": [85, 330, 1160, 800],
                "block_order": 1,
            },
            {
                "block_label": "footer",
                "block_content": "Physical page 1",
                "block_bbox": [85, 1600, 1160, 1650],
                "block_order": None,
            },
        ],
        "layout_det_res": {"score": 0.99999},
    }


def normalize(payload: dict, **kwargs):
    """Bind fixture output to separate source provenance without borrowing metadata text."""
    return normalize_vl(
        json.dumps(payload).encode(),
        borrower_pages(1)[0],
        pdf_sha256="a" * 64,
        image_sha256="b" * 64,
        models_sha256="c" * 64,
        generation_complete=True,
        **kwargs,
    )


def test_ocr_preserves_scope_markup_and_provenance_but_never_assumes_confidence() -> None:
    """A high layout score cannot allow unreviewed text through the existing 0.9 evidence gate."""
    artifact = normalize(output())
    original = borrower_pages(1)[0]
    assert artifact.metadata.tenant_id == original.tenant_id
    assert artifact.metadata.acl_groups == original.acl_groups
    assert artifact.metadata.document_version == original.document_version
    assert artifact.metadata.page == original.page
    assert artifact.metadata.extraction_confidence == 0
    assert artifact.recognition_confidence is None
    assert artifact.status == "REVIEW_REQUIRED"
    assert artifact.blocks[0].text.startswith("<table>")
    assert original.text not in artifact.metadata.text
    assert artifact.metadata.content_hash == text_hash(artifact.metadata.text)
    assert artifact.pdf_sha256 != artifact.image_sha256
    assert artifact.raw_sha256 == text_hash(json.dumps(output()))


@pytest.mark.parametrize(
    "change",
    [
        {"width": 0},
        {"width": 4000, "height": 4000},
        {"parsing_res_list": []},
        {"parsing_res_list": [{}]},
        {"parsing_res_list": None},
    ],
)
def test_invalid_or_blank_page_is_explicit_failure(change: dict) -> None:
    """An extraction failure cannot substitute the plausible text supplied in metadata."""
    with pytest.raises(ExtractionError):
        normalize(output() | change)


@pytest.mark.parametrize(
    "change",
    [
        {"block_bbox": [-1, 2, 5, 6]},
        {"block_bbox": [0, 2, 1241, 6]},
        {"block_bbox": [5, 2, 1, 6]},
        {"block_bbox": [0, 2, float("nan"), 6]},
        {"block_order": 2},
        {"block_order": 0},
        {"block_content": "x" * 100_001},
    ],
)
def test_invalid_block_geometry_order_or_size_is_rejected(change: dict) -> None:
    """Malformed bounds and incomplete order are not safe provenance for a review screen."""
    payload = output()
    payload["parsing_res_list"][0].update(change)
    with pytest.raises(ExtractionError):
        normalize(payload)


def test_whitespace_duplicate_order_and_excess_blocks_fail() -> None:
    """Enforce useful content, unique ordering and bounded work across the entire page."""
    payload = output()
    for block in payload["parsing_res_list"]:
        block["block_content"] = "  "
    with pytest.raises(ExtractionError):
        normalize(payload)
    payload = output()
    payload["parsing_res_list"][1]["block_order"] = 1
    with pytest.raises(ExtractionError):
        normalize(payload)
    payload["parsing_res_list"] = [output()["parsing_res_list"][1]] * 257
    with pytest.raises(ExtractionError):
        normalize(payload)


@pytest.mark.parametrize(
    "raw,complete",
    [(b"{}", False), (b"x" * 1_000_001, True), (b"[", True)],
    ids=["unverified-completion", "oversize", "invalid-json"],
)
def test_unverified_completion_oversize_and_invalid_json_fail(raw: bytes, complete: bool) -> None:
    """Token limits are not successful completion; incomplete OCR requires an explicit failure."""
    with pytest.raises(ExtractionError):
        normalize_vl(
            raw,
            borrower_pages(1)[0],
            pdf_sha256="a" * 64,
            image_sha256="b" * 64,
            models_sha256="c" * 64,
            generation_complete=complete,
        )
