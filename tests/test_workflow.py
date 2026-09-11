"""End-to-end local requests prove safe outcomes and independent evidence authorization."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from creditlens.api import create_app
from creditlens.domain import QueryRequest
from creditlens.settings import Settings
from creditlens.storage import audit_events, grants


@pytest.mark.parametrize(
    ("borrower", "expected"),
    [
        ("borrower-001", "MEETS_POLICY"),
        ("borrower-002", "EXCEPTION_REQUIRED"),
        ("borrower-003", "INSUFFICIENT_EVIDENCE"),
        ("borrower-004", "MATERIAL_CONFLICT"),
    ],
)
def test_packet_http_and_citation_access(borrower: str, expected: str) -> None:
    """The complete local packet path protects source links as well as generated output."""
    with TestClient(create_app(Settings(database_url="sqlite:///:memory:"))) as client:
        result = client.post(
            "/api/v1/query",
            json={
                "borrower_id": borrower,
                "question": "Prepare the DSCR underwriting packet",
                "effective_at": "2026-06-01",
            },
        )
        assert result.status_code == 200, result.text
        assert result.headers["Cache-Control"] == "no-store"
        packet = result.json()
        assert packet["policy_disposition"] == expected
        assert packet["provider_mode"] == "local-extractive"
        assert packet["cost_usd"] is None
        stages = [stage["name"] for stage in packet["stages"]]
        assert stages.index("authorization.filter_before_retrieval") < stages.index(
            "retrieval.local_bm25"
        )
        assert stages[-1] == "audit.persist"
        chunk = next(chunk for chunk in packet["evidence"] if chunk["borrower_id"] is not None)
        url = f"/api/v1/evidence/{chunk['chunk_id']}"
        assert (
            client.get(
                url, params={"borrower_id": borrower, "effective_at": "2026-06-01"}
            ).status_code
            == 200
        )
        assert (
            client.get(
                url, params={"borrower_id": "borrower-005", "effective_at": "2026-06-01"}
            ).status_code
            == 404
        )
        with client.app.state.store.engine.connect() as connection:
            record = connection.execute(select(audit_events)).mappings().one()
            assert record["request_id"] == packet["request_id"]
            assert "question" not in record["event"]
            assert record["event"]["corpus_version"] == packet["corpus_version"]
            assert record["event"]["protected_packet"]["request_id"] == packet["request_id"]
            assert len(record["event"]["packet_hash"]) == 64


def test_wrong_scope_and_out_of_domain_abstention() -> None:
    """IDOR and unrelated questions cannot produce a confident borrower assessment."""
    with TestClient(create_app(Settings(database_url="sqlite:///:memory:"))) as client:
        response = client.post(
            "/api/v1/query", json={"borrower_id": "borrower-999", "question": "DSCR review"}
        )
        assert response.status_code == 403
        response = client.post(
            "/api/v1/query", json={"borrower_id": "borrower-001", "question": "zzyyxxqq"}
        )
        assert response.json()["abstained"]
        assert response.json()["evidence"] == []
        response = client.post(
            "/api/v1/query",
            json={"borrower_id": "borrower-001", "question": "private-canary", "tenant_id": "evil"},
        )
        assert response.status_code == 422
        assert "private-canary" not in response.text


def test_audit_failure_prevents_acknowledgment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid packet is not successful when its required audit write fails."""
    with TestClient(create_app(Settings(database_url="sqlite:///:memory:"))) as client:

        def fail_record(*args: object, **kwargs: object) -> None:
            """Fail durable storage while leaving calculation and retrieval behavior intact."""
            raise OperationalError("private-secret", {}, Exception("private-canary"))

        monkeypatch.setattr(client.app.state.store, "record", fail_record)
        response = client.post(
            "/api/v1/query", json={"borrower_id": "borrower-001", "question": "DSCR review"}
        )
        assert response.status_code == 503
        assert "private-canary" not in response.text
        assert "private-secret" not in response.text


def test_grant_change_during_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A revocation after calculation is caught before the packet is delivered."""
    import creditlens.workflow as workflow_module
    from creditlens.errors import ServiceError

    with TestClient(create_app(Settings(database_url="sqlite:///:memory:"))) as client:
        store = client.app.state.store
        original = workflow_module.calculate_review

        def revoke_after_calculation(evidence: tuple) -> object:
            """Inject the race at the deterministic calculation seam for reproducibility."""
            result = original(evidence)
            with store.engine.begin() as connection:
                connection.execute(grants.update().values(enabled=False, revision=2))
            return result

        monkeypatch.setattr(workflow_module, "calculate_review", revoke_after_calculation)
        principal = store.resolve("synthetic-demo")
        with pytest.raises(ServiceError, match="access_denied"):
            client.app.state.workflow.query(
                QueryRequest(
                    borrower_id="borrower-001",
                    question="DSCR review",
                    effective_at=date(2026, 6, 1),
                ),
                principal,
            )


def test_body_and_rate_limits() -> None:
    """Oversized body rejection happens before JSON parsing and quota errors expose retry timing."""
    from creditlens.limits import QueryLimiter

    with TestClient(create_app(Settings(database_url="sqlite:///:memory:"))) as client:
        response = client.post("/api/v1/query", content=b"x" * 17000)
        assert response.status_code == 413
        assert response.headers["Cache-Control"] == "no-store"
        client.app.state.limiter = QueryLimiter(limit=1)
        body = {"borrower_id": "borrower-001", "question": "DSCR review"}
        assert client.post("/api/v1/query", json=body).status_code == 200
        response = client.post("/api/v1/query", json=body)
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"


def test_required_context_is_never_silently_truncated() -> None:
    """A late conflict in required multi-chunk evidence must not disappear behind a context cap."""
    from creditlens.corpus import build_demo_pages
    from creditlens.errors import ServiceError
    from creditlens.retrieval import chunk_page
    from creditlens.workflow import collect_context

    page = next(p for p in build_demo_pages() if p.section == "analyst_memo")
    long_page = page.model_copy(update={"text": "required analyst evidence " * 1000})
    chunks = chunk_page(long_page)
    assert len(chunks) > 16
    with pytest.raises(ServiceError, match="context_limit"):
        collect_context((), chunks, finance=True)
