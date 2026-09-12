"""Optional laboratory contracts use real numeric kernels without downloading model weights."""

from typing import Any

import pytest

from creditlens.corpus import build_demo_pages
from creditlens.llama_chunking import SemanticChunker, map_nodes, sentence_chunks, token_chunks
from creditlens.retrieval import chunk_page
from creditlens.retrieval_lab import DenseLab


def test_scoped_numpy_ranking() -> None:
    """A forbidden vector with the highest similarity cannot participate in an allowed ranking."""
    np = pytest.importorskip("numpy")
    lab = object.__new__(DenseLab)
    chunks = tuple(chunk_page(page)[0] for page in build_demo_pages()[:3])
    lab.indices = {chunk.chunk_id: i for i, chunk in enumerate(chunks)}
    lab.vectors = np.array([[0.8, 0.2], [0.1, 0.9], [1.0, 0.0]], dtype="float32")
    lab.query_vectors = {"query": np.array([[1.0, 0.0]], dtype="float32")}
    assert lab.rank("query", chunks[:2]) == chunks[:2]
    assert lab.rank("query", ()) == ()
    assert chunks[2] not in lab.rank("query", chunks[:2])


@pytest.mark.parametrize("parser", [sentence_chunks, token_chunks])
def test_llama_sentence_offsets(parser: Any) -> None:
    """Real LlamaIndex nodes retain page identity and exact source substrings."""
    pytest.importorskip("llama_index.core")
    page = build_demo_pages()[0].model_copy(
        update={"text": "Policy requires current evidence. " * 80}
    )
    chunks = parser(page, token_budget=64)
    assert len(chunks) > 1
    assert all(chunk.page == page.page for chunk in chunks)
    assert all(chunk.text == page.text[chunk.start_char : chunk.end_char] for chunk in chunks)
    assert chunks == parser(page, token_budget=64)


def test_exact_hnsw_agreement() -> None:
    """Scoped FAISS HNSW matches exact NumPy on a small fixed non-tied numeric control."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("faiss")
    lab = object.__new__(DenseLab)
    chunks = tuple(chunk_page(page)[0] for page in build_demo_pages()[:3])
    lab.indices = {chunk.chunk_id: i for i, chunk in enumerate(chunks)}
    lab.vectors = np.array([[1.0, 0.0], [0.8, 0.6], [0.0, 1.0]], dtype="float32")
    lab.query_vectors = {"query": np.array([[1.0, 0.0]], dtype="float32")}
    result: dict[str, Any] = lab.faiss_agreement("query", chunks, k=2)
    assert result["agreement_at_10"] == 1.0
    assert result["approximate_chunk_ids"] == result["exact_chunk_ids"]
    assert result["approximate_chunk_ids"] == [chunk.chunk_id for chunk in chunks[:2]]
    assert result["inner_products"] == pytest.approx([1.0, 0.8])
    scoped = lab.faiss_agreement("query", chunks[1:], k=2)
    assert chunks[0].chunk_id not in scoped["approximate_chunk_ids"]
    assert scoped["candidate_count"] == 2
    assert lab.faiss_agreement("query", ())["agreement_at_10"] is None


def test_parser_rejects_lost_evidence() -> None:
    """A parser omitting an entire source page must never return a usable empty chunk set."""
    with pytest.raises(ValueError, match="omitted trailing evidence"):
        map_nodes(build_demo_pages()[0], [], "invalid-parser")


def test_semantic_parameter_validation() -> None:
    """Invalid experiment thresholds fail before model imports or downloads."""
    from pathlib import Path

    with pytest.raises(ValueError, match="percentile"):
        SemanticChunker(Path("unused"), percentile=100)
