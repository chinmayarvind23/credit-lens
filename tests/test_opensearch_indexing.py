"""Administrative authorization, partial-write recovery and exact bulk acknowledgments."""

from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr

from creditlens.corpus import build_demo_pages
from creditlens.cortex_search import index_record
from creditlens.errors import ServiceError
from creditlens.governed_opensearch import GovernedOpenSearchProvider
from creditlens.opensearch_indexing import publish_batch, synchronize
from creditlens.retrieval import EvidenceCatalog
from creditlens.storage import GrantStore, grants, open_database


@pytest.fixture
def state():
    """Use real grant resolution while isolating the writer from backend transport tests."""
    engine = open_database("sqlite:///:memory:")
    store = GrantStore(engine)
    store.seed_demo()
    with engine.begin() as connection:
        connection.execute(grants.update().values(role="admin"))
    catalog = EvidenceCatalog(build_demo_pages())
    catalog.engine = engine
    catalog.catalog_id = "catalog"
    catalog.authority_id = "authority"
    principal = store.resolve("synthetic-demo")
    candidates, _ = catalog.snapshot(principal, "borrower-001", date(2026, 9, 1))
    index = SimpleNamespace(
        catalog=catalog,
        store=store,
        namespace="catalog",
        authority="authority",
        index="evidence-v1",
        schema=Mock(return_value={"mappings": {}}),
        check_ready=Mock(),
        search=Mock(),
        record=lambda chunk: index_record(chunk) | {"SEARCH_TEXT": chunk.text},
        request=Mock(),
    )
    yield SimpleNamespace(
        engine=engine, store=store, catalog=catalog, index=index, candidates=candidates
    )
    engine.dispose()


def acknowledged(batch):
    """Mirror actual per-document acknowledgments, including operation identity and shards."""
    return {
        "errors": False,
        "items": [
            {
                "index": {
                    "_index": "evidence-v1",
                    "_id": chunk.chunk_id,
                    "status": 201,
                    "result": "created",
                    "_shards": {"total": 1, "successful": 1, "failed": 0},
                }
            }
            for chunk in batch
        ],
    }


def run_sync(state, *, create=False):
    """Keep one consistent scope while each test changes a single trust boundary."""
    return synchronize(
        state.catalog,
        state.store,
        state.index,
        "synthetic-demo",
        "borrower-001",
        date(2026, 9, 1),
        create_index=create,
    )


def test_admin_publication_waits_for_visibility_and_accepts_retry(state):
    """Idempotent updates still require all writes and the reader's complete-scope proof."""
    import json

    reply = acknowledged(state.candidates)
    for item in reply["items"]:
        item["index"].update(status=200, result="updated")
    state.index.request.return_value = reply
    assert run_sync(state) == len(state.candidates)
    state.index.check_ready.assert_called_once_with()
    method, path = state.index.request.call_args.args
    assert method == "POST" and path == "/evidence-v1/_bulk?refresh=wait_for"
    content = state.index.request.call_args.kwargs["content"]
    assert content.endswith(b"\n")
    lines = [json.loads(line) for line in content.splitlines()]
    assert lines[::2] == [{"index": {"_id": c.chunk_id}} for c in state.candidates]
    assert lines[1::2] == [state.index.record(c) for c in state.candidates]
    state.index.search.assert_called_once()


def test_create_is_explicit_and_precedes_readiness(state):
    """An authorized create receives its own acknowledgment and never replaces an index."""
    state.index.request.side_effect = [
        {"acknowledged": True, "shards_acknowledged": True, "index": "evidence-v1"},
        acknowledged(state.candidates),
    ]
    assert run_sync(state, create=True) == len(state.candidates)
    assert state.index.request.call_args_list[0].args == (
        "PUT",
        "/evidence-v1",
        state.index.schema.return_value,
    )


@pytest.mark.parametrize("mutation", ["role", "disabled", "borrower"])
def test_unauthorized_scope_cannot_even_create_index(state, mutation):
    """Both the SQL admin role and borrower grant are resolved before remote side effects."""
    values = {
        "role": {"role": "underwriter"},
        "disabled": {"enabled": False},
        "borrower": {"borrower_ids": ["borrower-002"]},
    }[mutation]
    with state.engine.begin() as connection:
        connection.execute(grants.update().values(**values))
    with pytest.raises(ServiceError):
        run_sync(state, create=True)
    state.index.request.assert_not_called()
    state.index.check_ready.assert_not_called()


@pytest.mark.parametrize("field", ["catalog", "store", "namespace", "authority"])
def test_foreign_binding_cannot_write(state, field):
    """A writer cannot accidentally target another catalog, authority incarnation or grant pool."""
    setattr(state.index, field, "foreign")
    with pytest.raises(ValueError, match="same bound SQL"):
        run_sync(state, create=True)
    state.index.request.assert_not_called()


@pytest.mark.parametrize("phase", ["ready", "write", "verify"])
@pytest.mark.parametrize("mutation", ["grant", "catalog"])
def test_mid_publication_changes_are_detected(state, phase, mutation):
    """A partial write can remain, but a changed authority must never report completion."""

    def mutate(*args, **kwargs):
        """Inject a concurrent change at an actual outbound operation boundary."""
        if mutation == "grant":
            with state.engine.begin() as connection:
                connection.execute(grants.update().values(revision=2))
        else:
            state.catalog.revoke(state.candidates[0].chunk_id)
        return acknowledged(state.candidates)

    state.index.request.return_value = acknowledged(state.candidates)
    {"ready": state.index.check_ready, "write": state.index.request, "verify": state.index.search}[
        phase
    ].side_effect = mutate
    with pytest.raises(ServiceError, match="access_changed|evidence_changed"):
        run_sync(state)
    if phase == "ready":
        state.index.request.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    [
        "errors",
        "short",
        "duplicate",
        "wrong_index",
        "wrong_id",
        "failed_status",
        "bool_status",
        "failed_shards",
        "missing_shards",
        "zero_shards",
        "bool_shards",
        "wrong_result",
        "item_error",
        "wrong_operation",
        "wrong_wrapper",
    ],
)
def test_partial_or_malformed_bulk_cannot_complete(state, mutation):
    """Even errors=false cannot disguise a missing, foreign, failed or malformed operation."""
    reply = deepcopy(acknowledged(state.candidates))
    item = reply["items"][0]["index"]
    if mutation == "errors":
        reply["errors"] = True
    elif mutation == "short":
        reply["items"].pop()
    elif mutation == "duplicate":
        reply["items"][1] = deepcopy(reply["items"][0])
    elif mutation == "wrong_wrapper":
        reply = []
    elif mutation == "wrong_operation":
        reply["items"][0] = {"delete": item}
    else:
        key, value = {
            "wrong_index": ("_index", "another-index"),
            "wrong_id": ("_id", "another-record"),
            "failed_status": ("status", 409),
            "bool_status": ("status", True),
            "failed_shards": ("_shards", {"total": 2, "successful": 1, "failed": 1}),
            "missing_shards": ("_shards", {}),
            "zero_shards": ("_shards", {"total": 1, "successful": 0, "failed": 0}),
            "bool_shards": ("_shards", {"total": True, "successful": 1, "failed": 0}),
            "wrong_result": ("result", "deleted"),
            "item_error": ("error", {"reason": "private backend content"}),
        }[mutation]
        item[key] = value
    state.index.request.return_value = reply
    with pytest.raises(ServiceError, match="search_index_failed") as failure:
        run_sync(state)
    assert "private" not in str(failure.value)
    state.index.search.assert_not_called()


@pytest.mark.parametrize(
    "reply",
    [
        None,
        {},
        {"acknowledged": False},
        {
            "acknowledged": True,
            "shards_acknowledged": True,
            "index": "foreign",
        },
    ],
)
def test_bad_create_acknowledgment_stops_before_writes(state, reply):
    """A delayed or misdirected create is recoverable only through a later explicit run."""
    state.index.request.return_value = reply
    with pytest.raises(ServiceError, match="search_index_failed"):
        run_sync(state, create=True)
    state.index.check_ready.assert_not_called()
    state.index.search.assert_not_called()


@pytest.mark.parametrize("count", [0, 2, 101])
def test_batch_bounds_and_duplicate_ids_precede_transport(state, count):
    """Malformed work cannot bypass the fixed bounded write contract."""
    with pytest.raises(ValueError, match="indexing batch"):
        publish_batch(state.index, (state.candidates[0],) * count)
    state.index.request.assert_not_called()


def test_reader_visibility_failure_is_not_reported_as_success(state):
    """An acknowledged but incompletely searchable scope stays an operator-visible failure."""
    state.index.request.return_value = acknowledged(state.candidates)
    state.index.search.side_effect = ServiceError("search_index_incomplete", "Synchronize index")
    with pytest.raises(ServiceError, match="search_index_incomplete"):
        run_sync(state)


def test_real_provider_writer_transport_contract(state):
    """Execute the real provider transport so test doubles cannot invent its writer interface."""
    import json

    def handler(request):
        """Capture actual NDJSON serialization and return the documented bulk response."""
        assert request.method == "POST" and request.url.path == "/evidence-v1/_bulk"
        assert request.url.params["refresh"] == "wait_for"
        assert request.headers["Authorization"] == "Bearer fixture-only"
        assert request.headers["Content-Type"] == "application/x-ndjson"
        assert request.content.endswith(b"\n")
        assert (
            json.loads(request.content.splitlines()[1])["SEARCH_TEXT"] == state.candidates[0].text
        )
        return httpx.Response(200, json=acknowledged(state.candidates))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        index = GovernedOpenSearchProvider(
            "https://lexical.example.invalid",
            "evidence-v1",
            client,
            state.catalog,
            state.store,
            namespace="catalog",
            authority="authority",
            token=SecretStr("fixture-only"),
        )
        publish_batch(index, state.candidates)


def test_catalog_option_engine_keeps_shared_authority_pool(state):
    """SQL catalog isolation options wrap an engine without creating a different database pool."""
    state.catalog.engine = state.engine.execution_options(isolation_level="SERIALIZABLE")
    state.index.request.return_value = acknowledged(state.candidates)
    assert run_sync(state) == len(state.candidates)


def test_foreign_database_pool_cannot_publish(state):
    """Equal connection URLs do not establish the shared transactional authority boundary."""
    foreign = open_database("sqlite:///:memory:")
    try:
        state.catalog.engine = foreign
        with pytest.raises(ValueError, match="same bound SQL"):
            run_sync(state, create=True)
        state.index.request.assert_not_called()
    finally:
        foreign.dispose()
