"""Termination controls distinguish actual EOS from truncation without loading model weights."""

import pytest

from infra.ocr.completion import classify


@pytest.mark.parametrize("tokens", [[10, 11, 2], [10, 2, 0, 0]])
def test_early_eos(tokens) -> None:
    """Accept an observed EOS with optional batch padding and room before the cap."""
    assert classify(tokens, limit=8)["generation_complete"]


@pytest.mark.parametrize("tokens", [[10, 11, 12], [10, 11, 2], [10, 2, 11]])
def test_uncertain_or_truncated(tokens) -> None:
    """Missing EOS, EOS exactly at the cap and tokens after EOS cannot prove completion."""
    assert not classify(tokens, limit=3)["generation_complete"]


@pytest.mark.parametrize("tokens,limit", [([], 8), ([10] * 9, 8), ([True, 2], 8), ([2], 2048)])
def test_invalid_shape(tokens, limit) -> None:
    """Fail closed for shapes and generation bounds outside the reviewed probe contract."""
    with pytest.raises(ValueError):
        classify(tokens, limit=limit)
