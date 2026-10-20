"""Bearer-token verification and the users row behind it.

Two verifiers behind one interface:

* `FirebaseTokenVerifier`: Firebase Auth ID tokens (RS256), checked against Google's published
  keys for `CASELENS_FIREBASE_PROJECT_ID` (signature, iss, aud, exp, iat, sub).
* `DevTokenVerifier`: HS256 tokens signed with `CASELENS_DEV_AUTH_SECRET`, minted only by the CLI
  (`python -m caselens_api dev-token <subject>`). Allowed only when CASELENS_ENVIRONMENT=local;
  `build_verifier` refuses to start with a dev secret in any other environment.

After verification the API resolves (or creates, on first sign-in) the `users` row by
`oidc_subject` as `caselens_app`, which the users RLS policies allow.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any, Protocol

import jwt
from pydantic import BaseModel

from caselens_api.db import Conn
from caselens_api.problems import ProblemError, unauthorized
from caselens_api.settings import Settings

DEV_ISSUER = "caselens-local-dev"
DEV_AUDIENCE = "caselens-local"
DEV_ALGORITHM = "HS256"
MIN_DEV_SECRET_LENGTH = 32
FIREBASE_JWKS_URL = (
    "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"
)
CLOCK_SKEW_S = 30


class TokenIdentity(BaseModel):
    """What a verified token says about the caller."""

    subject: str
    email: str | None = None
    display_name: str | None = None


class TokenVerifier(Protocol):
    def verify(self, token: str) -> TokenIdentity: ...


class AuthError(Exception):
    """The token is missing, malformed, expired or not signed by a trusted issuer."""


def _identity(claims: dict[str, Any], *, trust_email: bool) -> TokenIdentity:
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub or len(sub) > 256:
        raise AuthError("token has no valid subject")
    email = claims.get("email") if trust_email else None
    name = claims.get("name")
    return TokenIdentity(
        subject=sub,
        email=email if isinstance(email, str) and email else None,
        display_name=name if isinstance(name, str) and name else None,
    )


class DevTokenVerifier:
    """Local-only HS256 verifier for CLI-minted dev tokens."""

    def __init__(self, secret: str) -> None:
        if len(secret) < MIN_DEV_SECRET_LENGTH:
            raise ValueError(
                f"CASELENS_DEV_AUTH_SECRET must be at least {MIN_DEV_SECRET_LENGTH} characters"
            )
        self._secret = secret

    def verify(self, token: str) -> TokenIdentity:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[DEV_ALGORITHM],
                audience=DEV_AUDIENCE,
                issuer=DEV_ISSUER,
                leeway=CLOCK_SKEW_S,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(str(exc)) from exc
        return _identity(claims, trust_email=True)

    def mint(
        self,
        subject: str,
        *,
        hours: float = 12.0,
        email: str | None = None,
        name: str | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": DEV_ISSUER,
            "aud": DEV_AUDIENCE,
            "sub": subject,
            "iat": now,
            "exp": now + int(hours * 3600),
        }
        if email:
            claims["email"] = email
        if name:
            claims["name"] = name
        return jwt.encode(claims, self._secret, algorithm=DEV_ALGORITHM)


class FirebaseTokenVerifier:
    """Firebase Auth ID tokens, verified locally against Google's JWKS (cached)."""

    def __init__(self, project_id: str, jwks_url: str = FIREBASE_JWKS_URL) -> None:
        self.project_id = project_id
        self.issuer = f"https://securetoken.google.com/{project_id}"
        self._jwks = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)

    def verify(self, token: str) -> TokenIdentity:
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.project_id,
                issuer=self.issuer,
                leeway=CLOCK_SKEW_S,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(str(exc)) from exc
        auth_time = claims.get("auth_time")
        if isinstance(auth_time, int | float) and auth_time > time.time() + CLOCK_SKEW_S:
            raise AuthError("auth_time is in the future")
        return _identity(claims, trust_email=claims.get("email_verified") is True)


class CompositeVerifier:
    """Dispatch on the token's `alg` header: HS256 -> dev verifier, RS256 -> Firebase."""

    def __init__(
        self, dev: DevTokenVerifier | None, firebase: FirebaseTokenVerifier | None
    ) -> None:
        self.dev = dev
        self.firebase = firebase

    def verify(self, token: str) -> TokenIdentity:
        try:
            alg = jwt.get_unverified_header(token).get("alg")
        except jwt.PyJWTError as exc:
            raise AuthError("malformed token") from exc
        if alg == DEV_ALGORITHM and self.dev is not None:
            return self.dev.verify(token)
        if alg == "RS256" and self.firebase is not None:
            return self.firebase.verify(token)
        raise AuthError("no verifier accepts this token")


def build_verifier(settings: Settings) -> CompositeVerifier:
    """Build the verifier for this environment. Raises at startup on an unsafe configuration."""
    dev: DevTokenVerifier | None = None
    secret = settings.dev_auth_secret.get_secret_value() if settings.dev_auth_secret else None
    if secret:
        if not settings.is_local:
            raise RuntimeError(
                "CASELENS_DEV_AUTH_SECRET is set but CASELENS_ENVIRONMENT is "
                f"{settings.environment!r}: the dev token verifier is only allowed locally"
            )
        dev = DevTokenVerifier(secret)
    firebase = (
        FirebaseTokenVerifier(settings.firebase_project_id)
        if settings.firebase_project_id
        else None
    )
    return CompositeVerifier(dev, firebase)


def bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise unauthorized("Missing bearer token.")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise unauthorized("The Authorization header must be 'Bearer <token>'.")
    return token.strip()


def verify_or_401(verifier: TokenVerifier, authorization: str | None) -> TokenIdentity:
    token = bearer_token(authorization)
    try:
        return verifier.verify(token)
    except AuthError as exc:
        raise unauthorized() from exc


# ---- users ---------------------------------------------------------------------------------


class UserRow(BaseModel):
    id: uuid.UUID
    email: str | None
    display_name: str | None
    is_demo: bool
    is_admin: bool
    created_at: datetime


_SELECT_USER = (
    "SELECT id, email, display_name, is_demo, is_admin, created_at "
    "FROM public.users WHERE oidc_subject = %s"
)
_INSERT_USER = (
    "INSERT INTO public.users (id, oidc_subject, email, display_name) "
    "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING"
)


def _select_user(conn: Conn, subject: str) -> UserRow | None:
    row = conn.execute(_SELECT_USER, (subject,)).fetchone()
    if row is None:
        return None
    return UserRow(
        id=row[0],
        email=row[1],
        display_name=row[2],
        is_demo=row[3],
        is_admin=row[4],
        created_at=row[5],
    )


def resolve_user(conn: Conn, identity: TokenIdentity) -> UserRow:
    """Find the user by oidc_subject, creating it on first sign-in. If the token's email already
    belongs to another user, the new row is created without an email."""
    user = _select_user(conn, identity.subject)
    if user is not None:
        return user
    for email in (identity.email, None):
        conn.execute(_INSERT_USER, (uuid.uuid4(), identity.subject, email, identity.display_name))
        user = _select_user(conn, identity.subject)
        if user is not None:
            return user
    raise ProblemError(503, "Could not create the user record.")
