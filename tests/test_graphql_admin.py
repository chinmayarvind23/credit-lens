"""Real signed-token HTTP tests for the optional administrator GraphQL explorer."""

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from creditlens.api import create_app
from creditlens.auth import Authenticator
from creditlens.domain import Principal
from creditlens.settings import Settings
from creditlens.storage import grants
from tests.test_auth import access_claims, production_settings


@pytest.fixture
def graphql_client():
    """Exercise real RSA verification and current SQL grants with an explicitly synthetic issuer."""
    pytest.importorskip("graphql")
    app = create_app(Settings(database_url="sqlite:///:memory:", graphql_enabled=True))
    with TestClient(app) as client:
        with app.state.store.engine.begin() as connection:
            connection.execute(
                grants.update().where(grants.c.subject == "synthetic-demo").values(role="admin")
            )
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        app.state.auth = Authenticator(
            production_settings(), app.state.store, lambda token: key.public_key()
        )
        token = jwt.encode(access_claims(), key, algorithm="RS256")
        client.headers["Authorization"] = "Bearer " + token
        yield client


def test_variables_fragments_catalog_and_own_audit_metadata(graphql_client):
    """Use a real workflow audit and canonical catalog, not mocked GraphQL resolver values."""
    client = graphql_client
    packet = client.post(
        "/api/v1/query",
        json={
            "borrower_id": "borrower-001",
            "question": "Calculate DSCR",
            "effective_at": "2026-09-11",
        },
    )
    assert packet.status_code == 200
    request_id = packet.json()["request_id"]
    body = {
        "query": """query Inspect($borrower: ID!, $at: String!) {
        me: viewer { ...Identity }
        catalog(borrowerId:$borrower, effectiveAt:$at) {
            revision documentCount pageCount chunkCount }
        recentAudits(borrowerId:$borrower, first:5) {
            requestId disposition createdAt searchProvider }
    } fragment Identity on Viewer { subject tenantId grantRevision }""",
        "variables": {"borrower": "borrower-001", "at": "2026-09-11"},
        "operationName": "Inspect",
    }
    response = client.post("/api/v1/admin/graphql", json=body)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    data = response.json()["data"]
    assert data["me"]["subject"] == "synthetic-demo"
    assert data["catalog"]["chunkCount"] >= data["catalog"]["pageCount"] > 0
    assert data["recentAudits"][0]["requestId"] == request_id
    assert set(data["recentAudits"][0]) == {
        "requestId",
        "disposition",
        "createdAt",
        "searchProvider",
    }
    assert "protected_packet" not in response.text and "Calculate DSCR" not in response.text


@pytest.mark.parametrize(
    "query",
    [
        "{ viewer { subject } "
        'catalog(borrowerId:"borrower-999",effectiveAt:"2026-09-11") { chunkCount } }',
        '{ recentAudits(borrowerId:"borrower-999") { requestId } }',
    ],
)
def test_denied_resolver_discards_all_partial_data(graphql_client, query):
    """A successful sibling resolver must not leak partial data when another scope check fails."""
    response = graphql_client.post("/api/v1/admin/graphql", json={"query": query})
    assert response.status_code == 403 and "data" not in response.json()


@pytest.mark.parametrize(
    "query",
    [
        "{ __schema { queryType { name } } }",
        "mutation { viewer { subject } }",
        "{ viewer { secret } }",
        "{ viewer { ...Cycle } } fragment Cycle on Viewer { ...Cycle }",
        '{ recentAudits(borrowerId:"borrower-001",first:51) { requestId } }',
        '{ catalog(borrowerId:"borrower-001",effectiveAt:"not-a-date") { pageCount } }',
        "{ " + " ".join(f"v{i}: viewer {{subject}}" for i in range(9)) + " }",
        "{ viewer { " + " ".join(f"s{i}: subject" for i in range(140)) + " } }",
    ],
)
def test_invalid_and_unbounded_documents_fail_without_reflecting_inputs(graphql_client, query):
    """Reject mutation, introspection, cycles, expansion and invalid values."""
    response = graphql_client.post("/api/v1/admin/graphql", json={"query": query})
    assert response.status_code == 422 and "data" not in response.json()
    assert query not in response.text


def test_current_role_and_revocation_are_checked_per_request(graphql_client):
    """A still-valid signed JWT must not preserve administrative access after grant changes."""
    client = graphql_client
    with client.app.state.store.engine.begin() as connection:
        connection.execute(
            grants.update().where(grants.c.subject == "synthetic-demo").values(role="underwriter")
        )
    assert (
        client.post("/api/v1/admin/graphql", json={"query": "{viewer{subject}}"}).status_code == 403
    )
    with client.app.state.store.engine.begin() as connection:
        connection.execute(
            grants.update().where(grants.c.subject == "synthetic-demo").values(enabled=False)
        )
    assert (
        client.post("/api/v1/admin/graphql", json={"query": "{viewer{subject}}"}).status_code == 403
    )


def test_cross_subject_audits_are_not_visible(graphql_client):
    """Administrator role does not grant visibility into another subject's audits."""
    client = graphql_client
    actor = client.app.state.store.resolve("synthetic-demo")
    other = Principal.model_validate(actor.model_dump() | {"subject": "other"})
    client.app.state.store.record(
        "other-request", other, {"borrower_id": "borrower-001", "disposition": "SECRET"}
    )
    result = client.post(
        "/api/v1/admin/graphql",
        json={"query": '{recentAudits(borrowerId:"borrower-001"){requestId}}'},
    )
    assert result.status_code == 200 and result.json()["data"]["recentAudits"] == []


def test_concurrent_grant_change_discards_result(graphql_client, monkeypatch):
    """Revoke the current grant after a real catalog read and verify the final authority barrier."""
    client = graphql_client
    catalog = client.app.state.workflow.catalog
    original = catalog.snapshot

    def snapshot(principal, borrower_id, effective_at):
        """Inject a real SQL revocation after actual canonical selection."""
        result = original(principal, borrower_id, effective_at)
        with client.app.state.store.engine.begin() as connection:
            connection.execute(
                grants.update().where(grants.c.subject == principal.subject).values(enabled=False)
            )
        return result

    monkeypatch.setattr(catalog, "snapshot", snapshot)
    response = client.post(
        "/api/v1/admin/graphql",
        json={"query": '{catalog(borrowerId:"borrower-001",effectiveAt:"2026-09-11"){pageCount}}'},
    )
    assert response.status_code == 403 and "data" not in response.json()


def test_default_public_demo_cannot_use_explorer():
    """The free synthetic browser identity never becomes an administrator through a new endpoint."""
    with TestClient(create_app(Settings(database_url="sqlite:///:memory:"))) as client:
        assert (
            client.post("/api/v1/admin/graphql", json={"query": "{viewer{subject}}"}).status_code
            == 403
        )


def test_disabled_and_invalid_operation_are_explicit(graphql_client):
    """Feature state and operation selection fail predictably without returning viewer data."""
    client = graphql_client
    response = client.post(
        "/api/v1/admin/graphql", json={"query": "query A{viewer{subject}} query B{viewer{subject}}"}
    )
    assert response.status_code == 422
    client.app.state.settings.graphql_enabled = False
    assert (
        client.post("/api/v1/admin/graphql", json={"query": "{viewer{subject}}"}).status_code == 503
    )


def test_disabled_ingestion_has_no_partial_viewer_result(graphql_client):
    """A disabled optional dependency cannot return a misleading partial success."""
    response = graphql_client.post(
        "/api/v1/admin/graphql",
        json={
            "query": "{viewer{subject} "
            'ingestionJob(id:"00000000-0000-0000-0000-000000000000"){state}}'
        },
    )
    assert response.status_code == 503 and "data" not in response.json()


def test_trusted_operator_cli_uses_existing_grants():
    """The usable local operator path needs neither public admin promotion nor a remote issuer."""
    import argparse
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from creditlens.storage import open_database
    from scripts.inspect_admin import inspect

    with TemporaryDirectory(prefix="creditlens-graphql-") as directory:
        root = Path(directory)
        settings = Settings(
            database_url="sqlite:///" + str(root / "audit.sqlite"), graphql_enabled=True
        )
        engine = open_database(settings.database_url)
        with engine.begin() as connection:
            connection.execute(
                grants.insert().values(
                    subject="operator",
                    tenant_id="demo-bank",
                    role="admin",
                    borrower_ids=["borrower-001"],
                    acl_groups=["underwriting"],
                    revision=1,
                    enabled=True,
                )
            )
        engine.dispose()
        query = root / "inspect.graphql"
        query.write_text(
            "{viewer{subject} "
            'catalog(borrowerId:"borrower-001",effectiveAt:"2026-09-11"){pageCount}}',
            encoding="utf-8",
        )
        args = argparse.Namespace(subject="operator", query=query, variables=None, operation=None)
        result = inspect(args, settings)["data"]
        assert result["viewer"]["subject"] == "operator" and result["catalog"]["pageCount"] > 0
