"""Governed lexical completeness, schema identity, authority and runtime ownership boundaries."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from creditlens.errors import ServiceError
from creditlens.governed_opensearch import GovernedOpenSearchProvider, record
from creditlens.settings import Settings
from creditlens.storage import grants
from tests.test_opensearch_provider import REQUEST
from tests.test_opensearch_provider import state as state
from tests.test_weaviate_provider import production_config


def envelope(hits):
    """Use the actual complete-shard envelope independently of adapter decoding."""
    return {
        "timed_out": False,
        "_shards": {"total": 1, "successful": 1, "failed": 0},
        "hits": {"hits": hits},
    }


@pytest.fixture
def governed(state):
    """Keep real canonical snapshots and SQL grants around the controlled remote boundary."""
    store, catalog, principal = state
    candidates, _ = catalog.snapshot(principal, REQUEST.borrower_id, REQUEST.effective_at)
    rig = SimpleNamespace(
        store=store,
        catalog=catalog,
        principal=principal,
        candidates=candidates,
        calls=[],
        mutation=None,
        change=None,
    )

    def handler(request):
        """Expose exact metadata and complete ranked scope, allowing one explicit hostile change."""
        rig.calls.append(request)
        assert request.headers["authorization"] == "Bearer fixture-token"
        assert request.headers["accept-encoding"] == "identity"
        if request.method == "GET":
            payload = copy.deepcopy(rig.metadata)
        else:
            body = json.loads(request.content)
            payload = (
                envelope([]) if body["query"] == {"match_none": {}} else copy.deepcopy(rig.reply)
            )
            if body["query"] != {"match_none": {}} and rig.change:
                rig.change()
        if rig.mutation:
            rig.mutation(payload, request)
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rig.provider = GovernedOpenSearchProvider(
        "https://search.example.invalid",
        "creditlens-test",
        client,
        catalog,
        store,
        namespace="catalog",
        authority="authority",
        token=SecretStr("fixture-token"),
    )
    schema = rig.provider.schema()
    rig.metadata = {
        "creditlens-test": {
            "mappings": schema["mappings"],
            "settings": {
                "index": {
                    "uuid": "index-uuid",
                    "similarity": schema["settings"]["similarity"],
                }
            },
        }
    }
    ordered = sorted(candidates, key=lambda chunk: chunk.chunk_id)
    rig.reply = envelope(
        [
            {
                "_index": "creditlens-test",
                "_id": chunk.chunk_id,
                "_score": 2.0 if i == 0 else 0.0,
                "_source": record(chunk),
            }
            for i, chunk in enumerate(ordered)
        ]
    )
    try:
        yield rig
    finally:
        client.close()


def test_single_search_proves_full_scope_before_keeping_matches(governed):
    """Nonmatching records prove visibility but do not become lexical candidates."""
    rig = governed
    result = rig.provider.search(REQUEST, rig.principal, 1)
    assert (
        len(result.chunks) == 1 and result.chunks[0].chunk_id == rig.reply["hits"]["hits"][0]["_id"]
    )
    request = rig.calls[-1]
    body = json.loads(request.content)
    assert body["size"] == len(rig.candidates)
    assert body["query"]["bool"]["minimum_should_match"] == 0
    assert body["query"]["bool"]["should"] == {"match": {"SEARCH_TEXT": REQUEST.question}}
    assert "must" not in body["query"]["bool"] and "SEARCH_TEXT" in body["_source"]
    assert request.url.params["allow_partial_search_results"] == "false"
    assert rig.provider.revision and len(rig.calls) == 3
    for hit in rig.reply["hits"]["hits"]:
        hit["_score"] = 0
    assert rig.provider.search(REQUEST, rig.principal).chunks == ()


@pytest.mark.parametrize("change", ["missing", "duplicate", "text", "extra", "score", "order"])
def test_partial_or_corrupt_search_never_looks_like_success(governed, change):
    """Full-set and canonical-text checks distinguish stale visibility from no lexical match."""
    hits = governed.reply["hits"]["hits"]
    if change == "missing":
        hits.pop()
    elif change == "duplicate":
        hits[-1] = copy.deepcopy(hits[0])
    elif change == "text":
        hits[0]["_source"]["SEARCH_TEXT"] = "Altered indexed evidence"
    elif change == "extra":
        hits[0]["_source"]["unexpected"] = "field"
    elif change == "score":
        hits[0]["_score"] = 10**1000
    else:
        hits.reverse()
    with pytest.raises(ServiceError, match="search_index_incomplete"):
        governed.provider.search(REQUEST, governed.principal)


@pytest.mark.parametrize(
    "change",
    ["alias", "namespace", "analyzer", "similarity", "analysis", "extra-option", "uuid", "shape"],
)
def test_readiness_rejects_unbound_or_changed_index_contract(governed, change):
    """A physical index, canonical authority and reviewed analysis contract are mandatory."""
    provider, index = governed.provider, governed.metadata["creditlens-test"]
    provider.check_ready()
    if change == "alias":
        governed.metadata["other-index"] = governed.metadata.pop("creditlens-test")
    elif change == "namespace":
        index["mappings"]["_meta"]["creditlens"]["catalog_id"] = "other"
    elif change == "analyzer":
        index["mappings"]["properties"]["SEARCH_TEXT"]["analyzer"] = "keyword"
    elif change == "similarity":
        index["settings"]["index"]["similarity"]["creditlens_bm25"]["b"] = "0.5"
    elif change == "analysis":
        index["settings"]["index"]["analysis"] = {
            "analyzer": {"standard": {"tokenizer": "keyword"}}
        }
    elif change == "extra-option":
        index["settings"]["index"]["similarity"]["creditlens_bm25"]["discount_overlaps"] = False
    elif change == "shape":
        index["settings"] = []
    else:
        index["settings"]["index"]["uuid"] = "new-index-uuid"
    with pytest.raises(ServiceError, match="search_index_incompatible"):
        provider.check_ready()


@pytest.mark.parametrize("change", ["grant", "catalog"])
def test_remote_success_cannot_hide_current_authority_change(governed, change):
    """The final SQL/canonical checks still run after successful complete search execution."""

    def revoke():
        """Change authority exactly while the controlled remote request executes."""
        if change == "catalog":
            governed.catalog.revoke(governed.candidates[0].chunk_id)
        else:
            with governed.store.engine.begin() as connection:
                connection.execute(grants.update().values(revision=2))

    governed.change = revoke
    with pytest.raises(ServiceError) as error:
        governed.provider.search(REQUEST, governed.principal)
    assert error.value.status == 409


def test_denied_grant_never_reaches_service_metadata(governed):
    """Authorize evidence scope before contacting even the lexical readiness endpoint."""
    with governed.store.engine.begin() as connection:
        connection.execute(grants.update().values(enabled=False))
    with pytest.raises(ServiceError):
        governed.provider.search(REQUEST, governed.principal)
    assert governed.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"opensearch_url": "http://127.0.0.1:19200"},
        {"opensearch_token": ""},
        {"opensearch_index": "alias,*"},
        {"production_lexical": "local"},
    ],
)
def test_unsafe_or_unused_lexical_settings_are_rejected(changes):
    """Credentials and explicit remote selection must form one governed HTTPS configuration."""
    values = production_config().model_dump() | {
        "production_lexical": "opensearch",
        "opensearch_url": "https://search.example.invalid",
        "opensearch_index": "creditlens-test",
        "opensearch_token": "fixture-token",
    }
    with pytest.raises(ValidationError):
        Settings(**(values | changes))


def test_selected_lexical_factory_and_cache_identity(governed, monkeypatch):
    """Exercise actual wrappers with a controlled SQL identity type and HTTP transport."""
    from creditlens import runtime, sql_catalog
    from creditlens.weaviate_provider import WeaviateHybridProvider

    rig = governed
    config = production_config().model_copy(
        update={
            "production_lexical": "opensearch",
            "governed_catalog_id": "catalog",
            "opensearch_url": rig.provider.endpoint,
            "opensearch_index": rig.provider.index,
            "opensearch_token": SecretStr("fixture-token"),
        }
    )
    rig.catalog.authority_id = "authority"
    monkeypatch.setattr(sql_catalog, "SqlEvidenceCatalog", type(rig.catalog))
    factory = Mock(return_value=rig.provider.client)
    monkeypatch.setattr(runtime, "ProviderClient", factory)
    vectors = SimpleNamespace(check_ready=Mock(), revision="vector-revision", rank=Mock())
    models = SimpleNamespace(
        revision="model-revision", rerank=lambda *args: (), encode_query=Mock()
    )
    with runtime.open_production_lexical(config, rig.catalog, rig.store) as lexical:
        assert isinstance(lexical, GovernedOpenSearchProvider)
        provider = WeaviateHybridProvider(rig.catalog, rig.store, vectors, models, lexical=lexical)
        provider.check_ready()
        original = runtime.production_cache_revision(config, provider)
        lexical.revision = "different-physical-index"
        assert runtime.production_cache_revision(config, provider) != original
        assert factory.call_args.kwargs["trust_env"] is False
        assert factory.call_args.kwargs["follow_redirects"] is False
    assert rig.provider.client.is_closed


def test_metadata_failure_closes_owned_factory_client(governed, monkeypatch):
    """A selected unavailable service must fail startup without retaining its HTTP pool."""
    from creditlens import runtime, sql_catalog

    rig = governed
    rig.catalog.authority_id = "authority"
    monkeypatch.setattr(sql_catalog, "SqlEvidenceCatalog", type(rig.catalog))
    monkeypatch.setattr(runtime, "ProviderClient", lambda **kwargs: rig.provider.client)
    rig.metadata = {}
    config = production_config().model_copy(
        update={
            "production_lexical": "opensearch",
            "opensearch_url": rig.provider.endpoint,
            "opensearch_index": rig.provider.index,
            "opensearch_token": SecretStr("fixture-token"),
        }
    )
    with pytest.raises(ServiceError, match="search_index_incompatible"):
        with runtime.open_production_lexical(config, rig.catalog, rig.store):
            pytest.fail("Unavailable lexical service must not yield a provider")
    assert rig.provider.client.is_closed


@pytest.mark.parametrize(
    "change",
    [
        {"token": SecretStr("")},
        {"namespace": ""},
        {"authority": ""},
        {"endpoint": "http://127.0.0.1:19200"},
    ],
)
def test_governed_provider_rejects_unsafe_construction(governed, change):
    """No operator or caller can enable anonymous, plaintext or unbound governed indexing."""
    values = dict(
        endpoint="https://search.example.invalid",
        index="creditlens-test",
        client=governed.provider.client,
        catalog=governed.catalog,
        store=governed.store,
        namespace="catalog",
        authority="authority",
        token=SecretStr("fixture-token"),
    )
    with pytest.raises(ValueError):
        GovernedOpenSearchProvider(**(values | change))
    assert governed.calls == []


@pytest.mark.parametrize("case", ["encoded", "oversized", "duplicate-json", "bad-json", "status"])
def test_governed_transport_redacts_unusable_replies(governed, case):
    """Untrusted service representation and HTTP failures cannot become raw API details."""

    def reply(request):
        """Use controlled malformed wire bytes without broadening the service endpoint."""
        if case == "encoded":
            return httpx.Response(200, content=b"{}", headers={"content-encoding": "custom"})
        if case == "oversized":
            return httpx.Response(200, content=b"x" * 1_048_577)
        if case == "duplicate-json":
            return httpx.Response(200, content=b'{"identity":1,"identity":2}')
        if case == "bad-json":
            return httpx.Response(200, content=b"not json")
        return httpx.Response(503, text="private upstream diagnostics")

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        governed.provider.client = client
        with pytest.raises(ServiceError, match="search_unavailable") as failure:
            governed.provider.request("GET", "/creditlens-test")
    assert "private" not in str(failure.value)


@pytest.mark.parametrize(
    "method,path,body,content",
    [
        ("DELETE", "/creditlens-test", None, None),
        ("GET", "/other-index", None, None),
        ("GET", "/creditlens-test-other", None, None),
        ("GET", "/creditlens-test#fragment", None, None),
        ("POST", "/creditlens-test/_bulk", {}, b"{}\n"),
    ],
)
def test_index_request_rejects_ambiguous_or_wrong_target(governed, method, path, body, content):
    """The common reader/writer transport cannot address a different physical index."""
    with pytest.raises(ValueError, match="configured physical index"):
        governed.provider.request(method, path, body, content=content)
    assert governed.calls == []


def test_bound_bulk_wire_preserves_ndjson_and_auth(governed):
    """The admin writer's explicit bytes use NDJSON without rewriting record identities."""
    calls = []

    def reply(request):
        """Observe the actual request produced by the common bounded transport."""
        calls.append(request)
        return httpx.Response(200, json={"errors": False, "items": []})

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        governed.provider.client = client
        assert (
            governed.provider.request(
                "POST", "/creditlens-test/_bulk?refresh=wait_for", content=b'{"index":{}}\n{}\n'
            )["errors"]
            is False
        )
    assert calls[0].headers["content-type"] == "application/x-ndjson"
    assert calls[0].content == b'{"index":{}}\n{}\n'
    assert calls[0].headers["authorization"] == "Bearer fixture-token"


@pytest.mark.parametrize("case", ["timed-out", "shards", "missing-hits", "unexpected-probe"])
def test_readiness_requires_empty_complete_query_execution(governed, case):
    """Mapping visibility alone cannot stand in for complete authorized search access."""

    def mutation(payload, request):
        """Fail the readiness search while leaving metadata valid."""
        if request.method != "POST":
            return
        if case == "timed-out":
            payload["timed_out"] = True
        elif case == "shards":
            payload["_shards"]["failed"] = 1
        elif case == "missing-hits":
            payload.pop("hits")
        else:
            payload["hits"]["hits"].append({"unexpected": "evidence"})

    governed.mutation = mutation
    with pytest.raises(ServiceError, match="search_index_incompatible"):
        governed.provider.check_ready()


def test_empty_authorized_scope_and_invalid_limit_avoid_network(governed):
    """An empty canonical set needs no service call, and invalid result bounds fail immediately."""
    for chunk in governed.candidates:
        governed.catalog.revoke(chunk.chunk_id)
    assert governed.provider.search(REQUEST, governed.principal).chunks == ()
    with pytest.raises(ValueError, match="between 1 and 100"):
        governed.provider.search(REQUEST, governed.principal, 0)
    assert governed.calls == []


def test_opensearch_selection_is_rejected_without_governed_weaviate():
    """Remote configuration must not be silently ignored by an unrelated runtime branch."""
    with pytest.raises(ValidationError, match="governed Weaviate"):
        Settings(
            production_lexical="opensearch",
            opensearch_url="https://search.example.invalid",
            opensearch_index="creditlens-test",
            opensearch_token=SecretStr("fixture-token"),
        )
