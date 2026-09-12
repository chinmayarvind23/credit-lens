"""Observe native generation termination before Paddle decodes away EOS and padding tokens."""

import json
from contextlib import contextmanager
from pathlib import Path


def classify(tokens: list[int], *, limit: int, eos: int = 2, pad: int = 0) -> dict:
    """An early EOS followed only by padding proves stopping; a token cap never does."""
    if not 1 <= limit <= 1024 or not tokens or len(tokens) > limit:
        raise ValueError("Unsupported generation length")
    if any(type(token) is not int or token < 0 for token in tokens):
        raise ValueError("Invalid generated token IDs")
    position = tokens.index(eos) if eos in tokens else None
    complete = (
        position is not None
        and position + 1 < limit
        and all(token == pad for token in tokens[position + 1 :])
    )
    return {
        "tokens": tokens,
        "eos_position": position,
        "max_new_tokens": limit,
        "generation_complete": complete,
    }


@contextmanager
def observe_generation(path: Path):
    """Wrap one pinned native class inside the owned probe; restore it on every exit path."""
    from paddlex.inference.models.doc_vlm.modeling.paddleocr_vl import (
        PaddleOCRVLForConditionalGeneration,
    )

    owner = PaddleOCRVLForConditionalGeneration
    original = owner.generate
    records = []

    def generate(model, inputs, **kwargs):
        """Preserve inference inputs/output while recording the actual undecoded token rows."""
        config = model.generation_config
        if (
            config.eos_token_id != 2
            or config.pad_token_id != 0
            or not config.trunc_input
            or config.forced_eos_token_id is not None
        ):
            raise ValueError("Unsupported native generation configuration")
        output = original(model, inputs, **kwargs)
        rows = output[0].tolist()
        if not rows or len(rows) > 256:
            raise ValueError("Unsupported native generation batch")
        records.extend(classify(row, limit=kwargs["max_new_tokens"]) for row in rows)
        path.write_text(json.dumps({"sequences": records}, indent=2), encoding="utf-8")
        return output

    owner.generate = generate
    try:
        yield records
    finally:
        owner.generate = original
