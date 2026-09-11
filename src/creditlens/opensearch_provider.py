"""Real OpenSearch lexical ranking constrained by current canonical authorization."""

import json
import math
import re
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from creditlens.cortex_search import (
    MAX_CANDIDATES,
    MAX_RESPONSE_BYTES,
    SHARED_SCOPE,
    decode_results,
    index_record,
)
from creditlens.domain import Chunk, Citation, Principal, QueryRequest
from creditlens.errors import ServiceError
from creditlens.retrieval import CanonicalCatalog
from creditlens.search_provider import SearchResult
from creditlens.storage import GrantStore


def validate_search_url(endpoint: str, index: str, local_http: bool) -> None:
    """Only explicitly enabled literal loopback endpoints may bypass transport encryption."""
    url = urlsplit(endpoint)
    local = local_http and url.scheme == "http" and url.hostname in ("127.0.0.1", "::1")
    if (
        not url.hostname
        or (url.scheme != "https" and not local)
        or url.username is not None
        or url.password is not None
        or url.path not in ("", "/")
        or url.query
        or url.fragment
        or re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", index) is None
    ):
        raise ValueError("Expected a configured HTTPS search host and one concrete index")


def lexical_body(
    request: QueryRequest, principal: Principal, candidates: tuple[Chunk, ...], limit: int
) -> dict[str, Any]:
    """Use match text as data and put all trusted scope in the pre-ranking filter context."""
    if not candidates or len(candidates) > MAX_CANDIDATES or not principal.acl_groups:
        raise ServiceError("search_scope_unavailable", "Search scope is unavailable")
    if request.effective_at == date.max:
        raise ServiceError("invalid_effective_date", "Effective date is unsupported", 422)
    effective = request.effective_at.isoformat()
    filters = [
        {"term": {"TENANT_ID": principal.tenant_id}},
        {"terms": {"BORROWER_SCOPE": [request.borrower_id, SHARED_SCOPE]}},
        {"terms": {"ACL_GROUPS": list(principal.acl_groups)}},
        {"range": {"VALID_FROM": {"lte": effective}}},
        {"range": {"VALID_TO_EXCLUSIVE": {"gt": effective}}},
        {"terms": {"CHUNK_ID": [chunk.chunk_id for chunk in candidates]}},
    ]
    return {
        "query": {
            "bool": {"must": {"match": {"SEARCH_TEXT": request.question}}, "filter": filters}
        },
        "_source": list(index_record(candidates[0])),
        "size": limit,
        "track_total_hits": False,
        "sort": [{"_score": "desc"}, {"CHUNK_ID": "asc"}],
    }


def decode_hits(
    payload: bytes, candidates: tuple[Chunk, ...], limit: int, index: str
) -> tuple[Chunk, ...]:
    """Partial shards and malformed hit wrappers fail before canonical provenance validation."""
    body = json.loads(payload)
    if not isinstance(body, dict) or body.get("timed_out") is not False:
        raise ValueError("Incomplete search response")
    shards = body.get("_shards")
    if not isinstance(shards, dict) or not valid_shards(shards):
        raise ValueError("Incomplete search shards")
    hits = body.get("hits")
    if not isinstance(hits, dict) or not isinstance(hits.get("hits"), list):
        raise ValueError("Invalid search hits")
    rows = [hit_source(hit, index) for hit in hits["hits"]]
    return decode_results(json.dumps({"results": rows}).encode(), candidates, limit)


def valid_shards(shards: dict[str, Any]) -> bool:
    """Strict integer counts distinguish successful execution from missing or Boolean status."""
    keys = ("total", "successful", "failed")
    if any(type(shards.get(key)) is not int for key in keys):
        return False
    return bool(
        shards["total"] > 0 and shards["failed"] == 0 and shards["successful"] == shards["total"]
    )


def hit_source(hit: Any, index: str) -> dict[str, Any]:
    """A hit must come from the configured physical index and name its exact canonical chunk."""
    if not isinstance(hit, dict) or not isinstance(hit.get("_source"), dict):
        raise ValueError("Invalid hit source")
    row: dict[str, Any] = hit["_source"]
    score = hit.get("_score")
    if (
        hit.get("_index") != index
        or hit.get("_id") != row.get("CHUNK_ID")
        or isinstance(score, bool)
        or not isinstance(score, int | float)
        or not math.isfinite(score)
        or score < 0
    ):
        raise ValueError("Invalid hit provenance")
    return row


class OpenSearchProvider:
    """The caller owns HTTP lifecycle and configures authentication for the fixed service host."""

    def __init__(
        self,
        endpoint: str,
        index: str,
        client: httpx.Client,
        catalog: CanonicalCatalog,
        store: GrantStore,
        *,
        token: SecretStr | None = None,
        timeout_seconds: float = 5,
        allow_local_http: bool = False,
    ) -> None:
        """Require HTTPS for remote services and explicit uncredentialed loopback test mode."""
        validate_search_url(endpoint, index, allow_local_http)
        if not 0 < timeout_seconds <= 30 or (endpoint.startswith("http:") and token is not None):
            raise ValueError("Invalid timeout or credentialed plaintext endpoint")
        self.endpoint = endpoint.rstrip("/")
        self.index = index
        self.client = client
        self.catalog = catalog
        self.store = store
        self._token = token
        self.timeout_seconds = timeout_seconds

    def verify(self, result: SearchResult) -> None:
        """Current grants and canonical catalog revisions take precedence over index freshness."""
        self.catalog.verify_revision(result.catalog_revision)
        if self.store.resolve(result.principal.subject) != result.principal:
            raise ServiceError("access_changed", "Access changed; retry the request", 409)

    def search(self, request: QueryRequest, principal: Principal, limit: int = 10) -> SearchResult:
        """Authorize before a single remote lexical ranking and reject changed access afterward."""
        if not 1 <= limit <= 100:
            raise ValueError("Search limit must be between 1 and 100")
        current = self.store.resolve(principal.subject)
        candidates, revision = self.catalog.snapshot(
            current, request.borrower_id, request.effective_at
        )
        checkpoint = SearchResult((), principal, request, revision, "opensearch-bm25")
        self.verify(checkpoint)
        if not candidates:
            return checkpoint
        body = lexical_body(request, current, candidates, limit)
        self.verify(checkpoint)
        try:
            chunks = decode_hits(self._request(body), candidates, limit, self.index)
        except (httpx.HTTPError, ValueError, RecursionError) as exc:
            raise ServiceError("search_unavailable", "Search is unavailable") from exc
        self.verify(checkpoint)
        return SearchResult(chunks, principal, request, revision, checkpoint.provider_mode)

    def _request(self, body: dict[str, Any]) -> bytes:
        """Reject partial/encoded responses and bound one attempt without broad fallback."""
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        milliseconds = max(1, int(self.timeout_seconds * 1000))
        params = {
            "allow_partial_search_results": "false",
            "cancel_after_time_interval": f"{milliseconds}ms",
        }
        body = body | {"timeout": f"{milliseconds}ms"}
        with self.client.stream(
            "POST",
            f"{self.endpoint}/{self.index}/_search",
            json=body,
            params=params,
            headers=headers,
            timeout=self.timeout_seconds,
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ValueError("Encoded responses are unsupported")
            payload = bytearray()
            for block in response.iter_bytes(chunk_size=8192):
                payload.extend(block)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise ValueError("Search response exceeds its byte limit")
        return bytes(payload)

    def citation(self, result: SearchResult, citation: Citation) -> Chunk:
        """Resolve only exact retrieved provenance against the current canonical candidate set."""
        self.verify(result)
        chunk = next((c for c in result.chunks if c.chunk_id == citation.chunk_id), None)
        if chunk is None or (chunk.document_id, chunk.document_version, chunk.page) != (
            citation.document_id,
            citation.document_version,
            citation.page,
        ):
            raise ServiceError("invalid_citation", "Citation is unavailable", 422)
        candidates, revision = self.catalog.snapshot(
            result.principal, result.request.borrower_id, result.request.effective_at
        )
        if chunk not in candidates or revision != result.catalog_revision:
            raise ServiceError("invalid_citation", "Citation is unavailable", 422)
        self.verify(result)
        return chunk
