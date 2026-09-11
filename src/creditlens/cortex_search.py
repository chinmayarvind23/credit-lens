"""Cortex REST contract with server-owned filters and canonical evidence hydration."""

import json
import re
from datetime import date
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from creditlens.domain import Chunk, Citation, Principal, QueryRequest
from creditlens.errors import ServiceError
from creditlens.retrieval import CanonicalCatalog
from creditlens.search_provider import SearchResult
from creditlens.storage import GrantStore

SHARED_SCOPE = ":shared-policy:"
MAX_CANDIDATES = 1024
MAX_RESPONSE_BYTES = 1_048_576


def index_record(chunk: Chunk) -> dict[str, Any]:
    """The index projection binds provenance to canonical text without returning remote text."""
    digest = sha256(
        json.dumps(chunk.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "CHUNK_ID": chunk.chunk_id,
        "TENANT_ID": chunk.tenant_id,
        "BORROWER_SCOPE": chunk.borrower_id or SHARED_SCOPE,
        "ACL_GROUPS": list(chunk.acl_groups),
        "VALID_FROM": chunk.valid_from.isoformat(),
        "VALID_TO_EXCLUSIVE": (chunk.valid_to or date.max).isoformat(),
        "DOCUMENT_ID": chunk.document_id,
        "DOCUMENT_VERSION": chunk.document_version,
        "PAGE": chunk.page,
        "CONTENT_HASH": chunk.content_hash,
        "START_CHAR": chunk.start_char,
        "END_CHAR": chunk.end_char,
        "RECORD_SHA256": digest,
    }


def validate_endpoint(endpoint: str) -> None:
    """Confine bearer credentials to a configured Snowflake HTTPS search endpoint."""
    url = urlsplit(endpoint)
    path = r"/api/v2/databases/[A-Za-z0-9_]+/schemas/[A-Za-z0-9_]+/"
    path += r"cortex-search-services/[A-Za-z0-9_]+:query"
    if (
        url.scheme != "https"
        or not (url.hostname or "").endswith(".snowflakecomputing.com")
        or url.port not in (None, 443)
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or re.fullmatch(path, url.path) is None
    ):
        raise ValueError("Expected a Snowflake HTTPS Cortex Search service endpoint")


def search_filter(
    request: QueryRequest, principal: Principal, candidates: tuple[Chunk, ...]
) -> dict[str, Any]:
    """Compile only trusted scope; the exact allowlist also excludes stale index grants."""
    if not candidates or len(candidates) > MAX_CANDIDATES or not principal.acl_groups:
        raise ServiceError("search_scope_unavailable", "Search scope is unavailable")
    if request.effective_at == date.max:
        raise ServiceError("invalid_effective_date", "Effective date is unsupported", 422)
    effective = request.effective_at.isoformat()
    return {
        "@and": [
            {"@eq": {"TENANT_ID": principal.tenant_id}},
            {
                "@or": [
                    {"@eq": {"BORROWER_SCOPE": request.borrower_id}},
                    {"@eq": {"BORROWER_SCOPE": SHARED_SCOPE}},
                ]
            },
            {"@or": [{"@contains": {"ACL_GROUPS": group}} for group in principal.acl_groups]},
            {"@lte": {"VALID_FROM": effective}},
            {"@not": {"@lte": {"VALID_TO_EXCLUSIVE": effective}}},
            {"@or": [{"@eq": {"CHUNK_ID": chunk.chunk_id}} for chunk in candidates]},
        ]
    }


def decode_results(payload: bytes, candidates: tuple[Chunk, ...], limit: int) -> tuple[Chunk, ...]:
    """Reject the entire remote result on any untrusted identity or metadata discrepancy."""
    body = json.loads(payload)
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise ValueError("Invalid result envelope")
    rows = body["results"]
    if len(rows) > limit:
        raise ValueError("Too many results")
    allowed = {chunk.chunk_id: chunk for chunk in candidates}
    result = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("CHUNK_ID"), str):
            raise ValueError("Invalid result identity")
        chunk = allowed.get(row["CHUNK_ID"])
        if chunk is None or chunk.chunk_id in seen:
            raise ValueError("Unknown or duplicate result")
        # Serialized equality distinguishes booleans from integers and forbids extra columns.
        if json.dumps(row, sort_keys=True) != json.dumps(index_record(chunk), sort_keys=True):
            raise ValueError("Invalid result provenance")
        result.append(chunk)
        seen.add(chunk.chunk_id)
    return tuple(result)


class CortexSearchProvider:
    """One bounded REST request ranks the authoritative currently allowed chunk set."""

    def __init__(
        self,
        endpoint: str,
        token: SecretStr,
        client: httpx.Client,
        catalog: CanonicalCatalog,
        store: GrantStore,
        timeout_seconds: float = 5,
    ) -> None:
        """Reuse an owned HTTP pool while keeping credentials out of result objects and errors."""
        validate_endpoint(endpoint)
        if not token.get_secret_value() or not 0 < timeout_seconds <= 30:
            raise ValueError("Search credentials and a bounded timeout are required")
        self.endpoint = endpoint
        self._token = token
        self.client = client
        self.catalog = catalog
        self.store = store
        self.timeout_seconds = timeout_seconds

    def verify(self, result: SearchResult) -> None:
        """Reject observed grant or source revocation before downstream use of evidence."""
        self.catalog.verify_revision(result.catalog_revision)
        if self.store.resolve(result.principal.subject) != result.principal:
            raise ServiceError("access_changed", "Access changed; retry the request", 409)

    def search(self, request: QueryRequest, principal: Principal, limit: int = 10) -> SearchResult:
        """Authorize independently of the caller's JWT-era principal before remote ranking."""
        if not 1 <= limit <= 100:
            raise ValueError("Search limit must be between 1 and 100")
        current = self.store.resolve(principal.subject)
        candidates, revision = self.catalog.snapshot(
            current, request.borrower_id, request.effective_at
        )
        checkpoint = SearchResult((), principal, request, revision, "snowflake-cortex-rest")
        self.verify(checkpoint)
        if not candidates:
            return checkpoint
        filters = search_filter(request, current, candidates)
        body = {
            "query": request.question,
            "columns": list(index_record(candidates[0])),
            "filter": filters,
            "limit": limit,
        }
        self.verify(checkpoint)
        try:
            payload = self._request(body)
            chunks = decode_results(payload, candidates, limit)
        except (httpx.HTTPError, ValueError, RecursionError) as exc:
            raise ServiceError("search_unavailable", "Search is unavailable") from exc
        self.verify(checkpoint)
        return SearchResult(chunks, principal, request, revision, checkpoint.provider_mode)

    def _request(self, body: dict[str, Any]) -> bytes:
        """Bound response bytes and reject encoded bodies without widening failed searches."""
        headers = {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        with self.client.stream(
            "POST",
            self.endpoint,
            json=body,
            headers=headers,
            follow_redirects=False,
            timeout=self.timeout_seconds,
        ) as response:
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ValueError("Encoded search responses are unsupported")
            payload = bytearray()
            for block in response.iter_bytes(chunk_size=8192):
                payload.extend(block)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise ValueError("Search response exceeds its byte limit")
        return bytes(payload)

    def citation(self, result: SearchResult, citation: Citation) -> Chunk:
        """Only exact provenance from this retrieval can become a currently authorized citation."""
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
