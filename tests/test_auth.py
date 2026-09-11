"""Exercise the identity boundary with real RSA signatures and current database grants."""

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr, ValidationError

from creditlens.auth import Authenticator, authorize_borrower, authorized_page
from creditlens.domain import Page, QueryRequest
from creditlens.errors import ServiceError
from creditlens.settings import Settings
from creditlens.storage import GrantStore, grants, open_database


@pytest.fixture
def store() -> Iterator[GrantStore]:
    """Use a shared in-memory database so revocation tests exercise SQL reads."""
    engine = open_database("sqlite:///:memory:")
    result = GrantStore(engine)
    result.seed_demo()
    yield result
    engine.dispose()


def production_settings() -> Settings:
    """An unreachable test origin prevents credentials or test requests reaching a provider."""
    return Settings(
        mode="production",
        database_url="sqlite:///:memory:",
        issuer="https://cognito-idp.us-east-1.amazonaws.com/test",
        client_id="client",
        cortex_url="https://example.invalid/query",
        cortex_token=SecretStr("test-only"),
    )


def access_claims() -> dict[str, object]:
    """Mint normal Cognito-shaped claims before changing one security dimension per case."""
    now = datetime.now(UTC)
    return {
        "sub": "synthetic-demo",
        "iss": production_settings().issuer,
        "client_id": "client",
        "token_use": "access",
        "scope": "creditlens/query",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }


@pytest.mark.parametrize(
    "change",
    [
        {"token_use": "id"},
        {"client_id": "wrong"},
        {"iss": "https://evil.invalid"},
        {"scope": "unrelated"},
        {"exp": 1},
        {"sub": ""},
        {"iat": 9999999999},
    ],
)
def test_bad_access_claims(store: GrantStore, change: dict[str, object]) -> None:
    """A valid RSA signature alone cannot authorize the wrong client, scope or token type."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = Authenticator(production_settings(), store, lambda token: key.public_key())
    token = jwt.encode(access_claims() | change, key, algorithm="RS256")
    with pytest.raises(ServiceError, match="unauthenticated"):
        auth.authenticate(f"Bearer {token}")


def test_signature_and_missing_token_fail(store: GrantStore) -> None:
    """Wrong-key and unsigned requests fail before a trusted identity is constructed."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = Authenticator(production_settings(), store, lambda token: key.public_key())
    token = jwt.encode(access_claims(), other, algorithm="RS256")
    for header in (None, "Basic abc", f"Bearer {token}", "Bearer " + "x" * 17000):
        with pytest.raises(ServiceError, match="unauthenticated"):
            auth.authenticate(header)


def test_live_grants_override_token_scope_and_revoke(store: GrantStore) -> None:
    """Untrusted tenant claims do not change permissions, and token reuse cannot undo revocation."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = Authenticator(production_settings(), store, lambda token: key.public_key())
    token = jwt.encode(
        access_claims() | {"tenant_id": "evil", "acl_groups": ["admin"]}, key, algorithm="RS256"
    )
    identity = auth.authenticate(f"Bearer {token}")
    assert identity.tenant_id == "demo-bank"
    assert identity.acl_groups == ("underwriting",)
    with store.engine.begin() as connection:
        connection.execute(grants.update().values(enabled=False, revision=2))
    with pytest.raises(ServiceError, match="access_denied"):
        auth.authenticate(f"Bearer {token}")


def sample_page() -> Page:
    """A policy page provides a fixed allowed control for one-dimension isolation tests."""
    return Page(
        tenant_id="demo-bank",
        borrower_id=None,
        document_id="policy",
        document_version="1",
        page=1,
        document_kind="policy",
        title="Policy",
        section="DSCR",
        text="Minimum DSCR is 1.25",
        acl_groups=("underwriting",),
        valid_from=date(2025, 1, 1),
        content_hash="test",
        parser_version="test",
    )


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "other"},
        {"borrower_id": "borrower-999"},
        {"acl_groups": ("restricted",)},
        {"valid_from": date(2027, 1, 1)},
        {"valid_to": date(2026, 1, 1)},
        {"extraction_confidence": 0.5},
    ],
)
def test_page_filters(store: GrantStore, change: dict[str, object]) -> None:
    """Each metadata boundary is enforced before ranking, including OCR confidence."""
    identity = store.resolve("synthetic-demo")
    page = sample_page()
    assert authorized_page(page, identity, "borrower-001", date(2026, 1, 1))
    assert not authorized_page(
        page.model_copy(update=change), identity, "borrower-001", date(2026, 1, 1)
    )


def test_borrower_and_request_scope(store: GrantStore) -> None:
    """Borrower IDOR and attempts to smuggle ACL arguments cannot reach retrieval."""
    with pytest.raises(ServiceError, match="access_denied"):
        authorize_borrower(store.resolve("synthetic-demo"), "borrower-999")
    with pytest.raises(ValidationError):
        QueryRequest.model_validate(
            {"borrower_id": "borrower-001", "question": "DSCR?", "tenant_id": "evil"}
        )


def test_mode_isolation() -> None:
    """Deployment configuration must never join public demo identity with governed data."""
    with pytest.raises(ValidationError):
        Settings(mode="demo", cortex_url="https://example.invalid")
    with pytest.raises(ValidationError):
        Settings(mode="production")


def test_jwks_key_resolution(store: GrantStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise PyJWT's JWK selection and signature path without a live Cognito dependency."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = Authenticator(production_settings(), store)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "test-key"
    assert auth.jwks is not None
    monkeypatch.setattr(auth.jwks, "fetch_data", lambda: {"keys": [jwk]})
    token = jwt.encode(access_claims(), key, algorithm="RS256", headers={"kid": "test-key"})
    assert auth.authenticate(f"Bearer {token}").subject == "synthetic-demo"
    demo = Authenticator(Settings(), store)
    with pytest.raises(ValueError, match="Demo identity"):
        demo._signing_key(token)
