"""Cognito verifies identity; current server-side grants define evidence access."""

from collections.abc import Callable
from typing import cast

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from jwt import PyJWKClient

from creditlens.access import authorize_borrower as authorize_borrower
from creditlens.access import authorized_page as authorized_page
from creditlens.domain import Principal
from creditlens.errors import ServiceError
from creditlens.settings import Settings
from creditlens.storage import GrantStore

SigningKey = str | RSAPublicKey


class Authenticator:
    """Pin issuer and algorithm; never discover a JWKS endpoint from untrusted JWT fields."""

    def __init__(
        self,
        settings: Settings,
        store: GrantStore,
        key_resolver: Callable[[str], SigningKey] | None = None,
    ) -> None:
        """A key seam permits RSA contract tests while production uses the configured pool."""
        self.settings = settings
        self.store = store
        self.jwks = (
            PyJWKClient(f"{settings.issuer}/.well-known/jwks.json", timeout=5)
            if settings.mode == "production"
            else None
        )
        self.key_resolver = key_resolver or self._signing_key

    def _signing_key(self, token: str) -> SigningKey:
        """Cache issuer keys briefly, with a bounded fetch and refresh for key rotation."""
        if self.jwks is None:
            raise ValueError("Demo identity has no signing key provider")
        return cast(SigningKey, self.jwks.get_signing_key_from_jwt(token).key)

    def authenticate(self, authorization: str | None) -> Principal:
        """Demo credentials have no path to production; all other requests need an access JWT."""
        if self.settings.mode == "demo":
            return self.store.resolve("synthetic-demo")
        if not authorization or not authorization.startswith("Bearer "):
            raise ServiceError("unauthenticated", "A valid access token is required", 401)
        token = authorization[7:]
        if len(token) > 16384:
            raise ServiceError("unauthenticated", "A valid access token is required", 401)
        try:
            claims = jwt.decode(
                token,
                self.key_resolver(token),
                algorithms=["RS256"],
                issuer=self.settings.issuer,
                options={
                    "verify_aud": False,
                    "require": ["sub", "exp", "iat", "iss", "token_use", "client_id"],
                },
            )
            self._validate_access_claims(claims)
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise ServiceError("unauthenticated", "A valid access token is required", 401) from exc
        return self.store.resolve(claims["sub"])

    def _validate_access_claims(self, claims: dict[str, object]) -> None:
        """Cognito access tokens bind client_id rather than the ID token's aud field."""
        if (
            claims.get("token_use") != "access"
            or claims.get("client_id") != self.settings.client_id
        ):
            raise ValueError("Wrong token type or client")
        scope = claims.get("scope", "")
        if not isinstance(scope, str) or self.settings.required_scope not in scope.split():
            raise ValueError("Missing required scope")
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise ValueError("Missing subject")
