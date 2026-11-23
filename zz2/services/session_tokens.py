"""Sessions this server issues itself — the only kind of session there is.

Every sign-in (password, Microsoft OpenID Connect, SAML) ends here: the server
mints an HS256 JWT signed with ``ZZ2_SESSION_SECRET`` (``ZZ2_SAML_SESSION_SECRET``
is accepted as a fallback for deployments that predate the Supabase exit).

The claim shape (``sub``, ``email``, ``app_metadata``, ``user_metadata``) is the
one :class:`~zz2.services.auth.AuthService` builds a principal from. Authorization
in ``app_metadata`` is a snapshot taken at sign-in; the auth service re-reads the
role from the user directory on each request where an account exists.

Tokens are revocable: sign-out records the ``jti`` until it would have expired.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from jose import ExpiredSignatureError, JWTError, jwt

from zz2.core.cache import TTLCache
from zz2.core.errors import AuthenticationError, CredentialExpired
from zz2.core.logging import logger
from zz2.settings import get_settings

ISSUER = "zz2"
LEGACY_ISSUERS = ("zz2-saml",)
ISSUERS = (ISSUER, *LEGACY_ISSUERS)
AUDIENCE = "zz2"
ALGORITHM = "HS256"

_revoked: TTLCache[str, bool] = TTLCache(7 * 24 * 3600, max_entries=50_000)


def is_session_token(token: str) -> bool:
    """Whether ``token`` claims to be one of ours. Routing only — not trust."""
    try:
        return jwt.get_unverified_claims(token).get("iss") in ISSUERS
    except JWTError:
        return False


def issue(*, subject: str, email: str, display: str, role: str,
          permissions: list[str], extra: dict[str, Any] | None = None,
          provider: str = "saml") -> str:
    settings = get_settings()
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": subject,
        "email": email,
        "iat": now,
        "nbf": now - 5,
        "exp": now + settings.session_ttl_seconds,
        "jti": uuid.uuid4().hex,
        "amr": [provider],
        "app_metadata": {"role": role, "permissions": permissions, "provider": provider},
        "user_metadata": {"display_name": display, **(extra or {})},
    }
    secret = settings.effective_session_secret
    if len(secret) < 32:
        raise AuthenticationError("Sign-in is not configured on this server.")
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def verify(token: str) -> dict[str, Any]:
    """Verify signature, issuer, audience, expiry and revocation.

    Raises:
        AuthenticationError: Any of them failed, or the sign-in method that
            issued the token has since been switched off — a token issued before
            an operator disabled SAML or Microsoft sign-in must stop working.
    """
    settings = get_settings()
    secret = settings.effective_session_secret
    if not secret:
        raise AuthenticationError("Sign-in is not configured. Sign in again.")
    try:
        claims = jwt.decode(
            token, secret, algorithms=[ALGORITHM], audience=AUDIENCE,
            options={"require_exp": True, "require_jti": True, "require_sub": True},
        )
    except JWTError as exc:
        logger.info("session token rejected", extra={"reason": type(exc).__name__})
        error = (CredentialExpired if isinstance(exc, ExpiredSignatureError)
                 else AuthenticationError)
        raise error("Your session has expired. Sign in again.") from exc
    if claims.get("iss") not in ISSUERS:
        raise AuthenticationError("Your session is not valid. Sign in again.")
    provider = (claims.get("app_metadata") or {}).get("provider") or "password"
    if (provider == "saml" and not settings.saml_enabled) or (
            provider == "microsoft" and not settings.microsoft_enabled):
        raise AuthenticationError("That sign-in method is disabled. Sign in again.")
    if _revoked.get(str(claims["jti"])):
        raise AuthenticationError("Your session has ended. Sign in again.")
    return claims


def revoke(token: str) -> bool:
    """Revoke a token of ours. Returns ``False`` for anything else."""
    try:
        claims = verify(token)
    except AuthenticationError:
        return False
    _revoked.set(str(claims["jti"]), True)
    return True


def reset() -> None:
    """Forget revocations. For tests."""
    _revoked.clear()
