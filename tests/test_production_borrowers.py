"""Production selector choices come from current grants, never demo business metadata."""

from collections.abc import Iterator

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select

from creditlens.api import create_app
from creditlens.auth import Authenticator
from creditlens.domain import Principal
from creditlens.storage import GrantStore, grants
from tests.test_auth import access_claims, production_settings


@pytest.fixture
def production_client() -> Iterator[TestClient]:
    """Use signed access tokens and real SQL grants without starting external dependencies."""
    with TestClient(create_app(production_settings())) as client:
        store: GrantStore = client.app.state.store
        with store.engine.begin() as connection:
            connection.execute(
                grants.insert(),
                [
                    {
                        "subject": "operator-one",
                        "tenant_id": "real-bank",
                        "role": "underwriter",
                        "borrower_ids": ["account-22", "borrower-001"],
                        "acl_groups": ["credit-team"],
                        "revision": 1,
                        "enabled": True,
                    },
                    {
                        "subject": "other-user",
                        "tenant_id": "other-bank",
                        "role": "underwriter",
                        "borrower_ids": ["foreign-account"],
                        "acl_groups": ["credit-team"],
                        "revision": 1,
                        "enabled": True,
                    },
                ],
            )
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        client.app.state.auth = Authenticator(
            production_settings(), store, lambda _token: key.public_key()
        )
        token = jwt.encode(
            access_claims() | {"sub": "operator-one", "borrower_ids": ["foreign-account"]},
            key,
            algorithm="RS256",
        )
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


def test_production_choices_use_only_current_grant_ids(
    production_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even an ID shared with the demo receives no fixture name or foreign-tenant metadata."""

    def forbid_demo() -> None:
        """Any production access to fictional business labels is a contract failure."""
        raise AssertionError("Production must not load demo borrower labels")

    monkeypatch.setattr("creditlens.api.build_demo_borrowers", forbid_demo)
    response = production_client.get("/api/v1/borrowers")
    assert response.status_code == 200
    assert response.json() == {
        "mode": "production",
        "borrowers": [
            {"borrower_id": "account-22", "name": "account-22", "industry": ""},
            {"borrower_id": "borrower-001", "name": "borrower-001", "industry": ""},
        ],
    }
    with production_client.app.state.store.engine.connect() as connection:
        assert (
            connection.execute(
                select(grants.c.subject).where(grants.c.subject == "synthetic-demo")
            ).first()
            is None
        )


def test_production_empty_scope_is_an_empty_selector(production_client: TestClient) -> None:
    """An authenticated principal without borrower grants receives no fallback fixture list."""
    store: GrantStore = production_client.app.state.store
    with store.engine.begin() as connection:
        connection.execute(
            grants.update().where(grants.c.subject == "operator-one").values(borrower_ids=[])
        )
    response = production_client.get("/api/v1/borrowers")
    assert response.status_code == 200
    assert response.json() == {"mode": "production", "borrowers": []}


@pytest.mark.parametrize("disabled", [False, True])
def test_production_choices_recheck_grants_before_return(
    production_client: TestClient, monkeypatch: pytest.MonkeyPatch, disabled: bool
) -> None:
    """Revocation or changed scope after authentication prevents release of stale IDs."""
    store: GrantStore = production_client.app.state.store
    resolve = store.resolve
    calls = 0

    def revoke_after_authentication(subject: str) -> Principal:
        """Change the SQL grant after its first read to expose the endpoint's second check."""
        nonlocal calls
        principal = resolve(subject)
        calls += 1
        if calls == 1:
            with store.engine.begin() as connection:
                connection.execute(
                    grants.update()
                    .where(grants.c.subject == subject)
                    .values(borrower_ids=[], enabled=not disabled, revision=2)
                )
        return principal

    monkeypatch.setattr(store, "resolve", revoke_after_authentication)
    response = production_client.get("/api/v1/borrowers")
    assert response.status_code == (403 if disabled else 409)
    assert "borrowers" not in response.json()
    assert "account-22" not in response.text
    assert "borrower-001" not in response.text


def test_production_revocation_applies_on_next_request(production_client: TestClient) -> None:
    """Reusing an unexpired signed token cannot retain a revoked borrower selector."""
    assert production_client.get("/api/v1/borrowers").status_code == 200
    store: GrantStore = production_client.app.state.store
    with store.engine.begin() as connection:
        connection.execute(
            grants.update().where(grants.c.subject == "operator-one").values(enabled=False)
        )
    assert production_client.get("/api/v1/borrowers").status_code == 403
