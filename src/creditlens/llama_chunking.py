"""LlamaIndex sentence parsing remains subordinate to canonical physical-page provenance."""

import importlib
import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from creditlens.domain import Chunk, Page
from creditlens.retrieval_lab import EMBED_MODEL, EMBED_REVISION, model_snapshot


def sentence_chunks(page: Page, token_budget: int = 256) -> tuple[Chunk, ...]:
    """Map every LlamaIndex node back to exact source bytes; never invent source offsets."""
    if not 64 <= token_budget <= 2048:
        raise ValueError("Sentence chunk token budget must be between 64 and 2048")
    parser_module = importlib.import_module("llama_index.core.node_parser")
    schema = importlib.import_module("llama_index.core.schema")
    parser = parser_module.SentenceSplitter(
        chunk_size=token_budget,
        chunk_overlap=0,
        include_metadata=False,
        include_prev_next_rel=False,
    )
    nodes = parser.get_nodes_from_documents([schema.Document(text=page.text)])
    return map_nodes(page, nodes, f"llama-sentence-v1-{token_budget}")


def map_nodes(page: Page, nodes: list[Any], chunker: str) -> tuple[Chunk, ...]:
    """Preserve physical-page spans for either parser and fail on lost or altered evidence."""
    result = []
    cursor = 0
    for node in nodes:
        text = str(node.get_content())
        start = page.text.find(text, cursor)
        if not text or start < 0 or page.text[cursor:start].strip():
            raise ValueError("Sentence parser did not preserve exact source evidence")
        end = start + len(text)
        identity = json.dumps(
            [
                page.tenant_id,
                page.document_id,
                page.document_version,
                page.page,
                page.content_hash,
                start,
                end,
                chunker,
            ]
        )
        result.append(
            Chunk.model_validate(
                page.model_dump()
                | {
                    "text": text,
                    "chunk_id": sha256(identity.encode()).hexdigest(),
                    "start_char": start,
                    "end_char": end,
                    "chunker_version": chunker,
                }
            )
        )
        cursor = end
    if page.text[cursor:].strip():
        raise ValueError("Sentence parser omitted trailing evidence")
    return tuple(result)


class SemanticChunker:
    """Keep model loading outside page iteration and split only within one canonical page."""

    def __init__(self, cache: Path, percentile: int = 95) -> None:
        """Pin embeddings and keep semantic thresholds explicit experiment parameters."""
        if not 50 <= percentile <= 99:
            raise ValueError("Semantic breakpoint percentile must be between 50 and 99")
        embeddings = importlib.import_module("llama_index.embeddings.huggingface")
        parsers = importlib.import_module("llama_index.core.node_parser")
        snapshot = model_snapshot(EMBED_MODEL, EMBED_REVISION, cache / "embedding")
        model = embeddings.HuggingFaceEmbedding(
            model_name=snapshot,
            device="cpu",
            trust_remote_code=False,
            model_kwargs={"use_safetensors": True},
            embed_batch_size=64,
        )
        self.parser = parsers.SemanticSplitterNodeParser(
            embed_model=model,
            buffer_size=1,
            breakpoint_percentile_threshold=percentile,
            include_metadata=False,
            include_prev_next_rel=False,
        )
        self.chunker = f"llama-semantic-v1-{percentile}-{EMBED_REVISION}"

    def chunks(self, page: Page) -> tuple[Chunk, ...]:
        """Use real semantic boundaries while rejecting any node that breaks exact citations."""
        schema = importlib.import_module("llama_index.core.schema")
        nodes = self.parser.get_nodes_from_documents([schema.Document(text=page.text)])
        return map_nodes(page, nodes, self.chunker)
