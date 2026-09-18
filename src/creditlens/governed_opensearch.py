"""Require a bound physical lexical index and complete current canonical search visibility."""

import json
import re
from hashlib import sha256
from typing import Any, cast

import httpx
from pydantic import SecretStr

from creditlens.cortex_search import MAX_RESPONSE_BYTES, decode_results, index_record
from creditlens.domain import Chunk, Principal, QueryRequest
from creditlens.errors import ServiceError
from creditlens.opensearch_provider import (
    OpenSearchProvider,
    hit_source,
    lexical_body,
    valid_shards,
)
from creditlens.retrieval import CanonicalCatalog
from creditlens.search_provider import SearchResult
from creditlens.storage import GrantStore

LEXICAL_CONTRACT = "canonical-opensearch-bm25-v1"


def record(chunk: Chunk) -> dict[str, Any]:
    """The stored lexical text must match the same full canonical record as its provenance."""
    return index_record(chunk) | {"SEARCH_TEXT": chunk.text}


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON objects at the untrusted search-service boundary."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate response key")
        result[key] = value
    return result


class GovernedOpenSearchProvider(OpenSearchProvider):
    """The caller owns a separate verified TLS client; SQL remains the evidence authority."""

    def __init__(
        self,
        endpoint: str,
        index: str,
        client: httpx.Client,
        catalog: CanonicalCatalog,
        store: GrantStore,
        *,
        namespace: str,
        authority: str,
        token: SecretStr,
        timeout_seconds: float = 5,
    ) -> None:
        """Bind authentication and captured catalog identity before any service operation."""
        if not token.get_secret_value() or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", namespace):
            raise ValueError("Expected authenticated governed lexical configuration")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", authority):
            raise ValueError("Expected immutable catalog authority")
        super().__init__(
            endpoint, index, client, catalog, store, token=token, timeout_seconds=timeout_seconds
        )
        self.namespace, self.authority, self.revision = namespace, authority, ""
        self.token = token

    def schema(self) -> dict[str, Any]:
        """Share the strict lexical projection and namespace binding with the authorized writer."""
        keywords = (
            "CHUNK_ID",
            "TENANT_ID",
            "BORROWER_SCOPE",
            "ACL_GROUPS",
            "DOCUMENT_ID",
            "DOCUMENT_VERSION",
            "CONTENT_HASH",
            "RECORD_SHA256",
        )
        properties: dict[str, Any] = {name: {"type": "keyword"} for name in keywords}
        properties.update(
            {name: {"type": "integer"} for name in ("PAGE", "START_CHAR", "END_CHAR")}
        )
        properties.update(
            {
                name: {"type": "date", "format": "strict_date"}
                for name in ("VALID_FROM", "VALID_TO_EXCLUSIVE")
            }
        )
        properties["SEARCH_TEXT"] = {
            "type": "text",
            "analyzer": "standard",
            "similarity": "creditlens_bm25",
        }
        return {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
                "similarity": {"creditlens_bm25": {"type": "BM25", "k1": 1.2, "b": 0.75}},
            },
            "mappings": {
                "dynamic": "strict",
                "properties": properties,
                "_meta": {
                    "creditlens": {
                        "catalog_id": self.namespace,
                        "authority": self.authority,
                        "contract": LEXICAL_CONTRACT,
                    }
                },
            },
        }

    def request(
        self, method: str, path: str, body: Any = None, *, content: bytes | None = None
    ) -> Any:
        """Bound JSON and bulk responses while keeping credentials on the configured index host."""
        if (
            method not in {"GET", "POST", "PUT"}
            or not path.startswith(f"/{self.index}")
            or path.split("?", 1)[0].split("/", 2)[1] != self.index
            or "#" in path
            or (body is not None and content is not None)
        ):
            raise ValueError("Expected an operation on the configured physical index")
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {self.token.get_secret_value()}",
        }
        if content is not None:
            headers["Content-Type"] = "application/x-ndjson"
        try:
            with self.client.stream(
                method,
                self.endpoint + path,
                json=body,
                content=content,
                headers=headers,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise ValueError("Encoded search response")
                payload = bytearray()
                for block in response.iter_bytes(chunk_size=8192):
                    payload.extend(block)
                    if len(payload) > MAX_RESPONSE_BYTES:
                        raise ValueError("Search response exceeds budget")
            return json.loads(payload, object_pairs_hook=unique_object)
        except (httpx.HTTPError, ValueError, RecursionError) as error:
            raise ServiceError("search_unavailable", "Search is unavailable") from error

    def check_ready(self) -> None:
        """Pin the physical index, mapping and BM25 contract, and exercise reader search access."""
        try:
            _ = self.catalog.version
            metadata = self.request("GET", f"/{self.index}")
            if not isinstance(metadata, dict) or set(metadata) != {self.index}:
                raise ValueError("Expected one physical index")
            index = metadata[self.index]
            settings = index["settings"]["index"]
            similarity = settings["similarity"]["creditlens_bm25"]
            if (
                not isinstance(similarity, dict)
                or set(similarity) != {"type", "k1", "b"}
                or settings.get("analysis")
                or index["mappings"] != self.schema()["mappings"]
                or similarity.get("type") != "BM25"
                or str(similarity.get("k1")) != "1.2"
                or str(similarity.get("b")) != "0.75"
                or not isinstance(settings.get("uuid"), str)
                or not settings["uuid"]
            ):
                raise ValueError("Incompatible lexical index contract")
            revision = sha256(
                json.dumps(
                    [
                        LEXICAL_CONTRACT,
                        self.namespace,
                        self.authority,
                        settings["uuid"],
                        index["mappings"],
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if self.revision and revision != self.revision:
                raise ValueError("Lexical index identity changed")
            probe = self._hits(
                self.request(
                    "POST",
                    self._search_path(),
                    {
                        "query": {"match_none": {}},
                        "size": 0,
                        "track_total_hits": False,
                    },
                )
            )
            if probe:
                raise ValueError("Unexpected readiness evidence")
            self.revision = revision
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ServiceError(
                "search_index_incompatible", "Lexical index is unavailable"
            ) from error

    def _search_path(self) -> str:
        """Disable partial shard results and apply the service's own bounded cancellation."""
        milliseconds = max(1, int(self.timeout_seconds * 1000))
        return (
            f"/{self.index}/_search?allow_partial_search_results=false"
            f"&cancel_after_time_interval={milliseconds}ms"
        )

    def _hits(self, payload: Any) -> list[Any]:
        """Reject incomplete execution before any result or readiness interpretation."""
        if (
            not isinstance(payload, dict)
            or payload.get("timed_out") is not False
            or not isinstance(payload.get("_shards"), dict)
            or not valid_shards(payload["_shards"])
        ):
            raise ValueError("Incomplete lexical search")
        hits = payload.get("hits")
        if not isinstance(hits, dict) or not isinstance(hits.get("hits"), list):
            raise ValueError("Invalid lexical hits")
        return cast(list[Any], hits["hits"])

    def search(self, request: QueryRequest, principal: Principal, limit: int = 10) -> SearchResult:
        """Prove complete canonical visibility and lexical ordering in the same bounded search."""
        if not 1 <= limit <= 100:
            raise ValueError("Search limit must be between 1 and 100")
        current = self.store.resolve(principal.subject)
        candidates, revision = self.catalog.snapshot(
            current, request.borrower_id, request.effective_at
        )
        checkpoint = SearchResult((), principal, request, revision, "opensearch-governed-bm25-v1")
        self.verify(checkpoint)
        if not candidates:
            return checkpoint
        body = lexical_body(request, current, candidates, len(candidates))
        query = body["query"]["bool"]
        query["should"], query["minimum_should_match"] = query.pop("must"), 0
        body["_source"] = list(record(candidates[0]))
        body["timeout"] = f"{max(1, int(self.timeout_seconds * 1000))}ms"
        self.check_ready()
        self.verify(checkpoint)
        try:
            selected = self._decode(
                self.request("POST", self._search_path(), body), candidates, limit
            )
        except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as error:
            raise ServiceError("search_index_incomplete", "Lexical index is unavailable") from error
        self.verify(checkpoint)
        return SearchResult(selected, principal, request, revision, checkpoint.provider_mode)

    def _decode(self, payload: Any, candidates: tuple[Chunk, ...], limit: int) -> tuple[Chunk, ...]:
        """Canonical text and exact set equality precede keeping positive-score lexical matches."""
        hits = self._hits(payload)
        if len(hits) != len(candidates):
            raise ValueError("Incomplete canonical lexical scope")
        rows = [hit_source(hit, self.index) for hit in hits]
        chunks = decode_results(
            json.dumps(
                {
                    "results": [
                        {key: value for key, value in row.items() if key != "SEARCH_TEXT"}
                        for row in rows
                    ]
                }
            ).encode(),
            candidates,
            len(candidates),
        )
        order = [(-hit["_score"], chunk.chunk_id) for hit, chunk in zip(hits, chunks, strict=True)]
        if order != sorted(order) or any(
            row.get("SEARCH_TEXT") != chunk.text for row, chunk in zip(rows, chunks, strict=True)
        ):
            raise ValueError("Invalid lexical ordering or source text")
        return tuple(chunk for hit, chunk in zip(hits, chunks, strict=True) if hit["_score"] > 0)[
            :limit
        ]
