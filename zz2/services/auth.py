"""Authentication: access token to :class:`Principal`.

Supabase Auth is the only identity provider. The legacy path this replaces — a
pure-Python PBKDF2 implementation, ``credentials.json``, ``sessions.json``, a
disk-persisted lockout table and hand-rolled constant-time comparison — was
roughly 600 lines of security-critical code maintained in-house because Python
2.7 on the NAS could not do better. None of it needed to survive the move to a
VPS, and every line of it was a line that could be wrong.

Two verification strategies, chosen by configuration:

**Local (preferred).** The JWT's HS256 signature is verified in-process with
``ZZ2_SUPABASE_JWT_SECRET``. No network call, so authentication does not fail when
Supabase is briefly unreachable, and a page issuing twelve parallel API calls
costs zero upstream requests.

**Remote (fallback).** The token is presented to ``/auth/v1/user``. Correct
without the JWT secret, but it puts a network round-trip on every request. Results
are cached briefly — the monolith did this too, and had to, but its cache was an
unbounded dict swept only on insert.

Tokens are read from the ``Authorization`` header **only**. The monolith also
accepted ``?token=`` from the query string, which writes a live session token into
access logs, ``Referer`` headers on every outbound link, browser history and any
intermediate proxy's logs. That is the single highest-value change in this file.
"""

from __future__ import annotations

from typing import Any

from jose import JWTError, jwt

from zz2.core.cache import TTLCache
from zz2.core.errors import AuthenticationError, UpstreamError
from zz2.core.logging import audit, logger
from zz2.core.text import as_text
from zz2.domain.permissions import Principal, parse_permissions
from zz2.gateways.supabase import supabase
from zz2.repositories.roles import RoleRepository
from zz2.settings import Settings, get_settings

PRINCIPAL_CACHE_TTL = 60.0
"""Seconds a resolved principal is reused.

Short by design. A role change or a deactivated account must take effect
promptly, and the whole point of resolving roles from local storage on each
request is that an administrator's change does not require the user to sign in
again.
"""

_principal_cache: TTLCache[str, Principal] = TTLCache(PRINCIPAL_CACHE_TTL, max_entries=512)


class AuthService:
    """Resolves bearer tokens into principals."""

    def __init__(self, roles: RoleRepository | None = None) -> None:
        self._roles = roles or RoleRepository()

    @property
    def _settings(self) -> Settings:
        """Settings read at use time, not captured in ``__init__``.

        This object is a module-level singleton constructed at import, so a
        snapshot taken in the constructor would freeze whatever configuration
        happened to exist when the module was first imported. ``get_settings`` is
        memoised, so reading it per call costs nothing — and it makes the service
        respond to a reconfiguration instead of silently ignoring one.
        """
        return get_settings()

    # -- Public API ------------------------------------------------------------
    @staticmethod
    def token_from_header(header_value: str | None) -> str | None:
        """Extract a bearer token from an ``Authorization`` header.

        Returns ``None`` for a missing, malformed or non-bearer header. Callers
        distinguish "no credentials" from "bad credentials" — the first is a
        signed-out browser, the second is worth auditing.
        """
        if not header_value:
            return None
        scheme, _, token = header_value.partition(" ")
        if scheme.lower() != "bearer":
            return None
        token = token.strip()
        return token or None

    def authenticate(self, token: str) -> Principal:
        """Resolve a token, using the cache when possible.

        Raises:
            AuthenticationError: The token is absent, malformed, expired or
                rejected upstream.
            UpstreamError: Remote verification was configured but unreachable.
        """
        if not token:
            raise AuthenticationError()

        cached = _principal_cache.get(token)
        if cached is not None:
            return cached

        claims = self._verify(token)
        principal = self._build_principal(claims)
        _principal_cache.set(token, principal)
        return principal

    @staticmethod
    def invalidate(token: str) -> None:
        """Drop a cached principal, e.g. on sign-out."""
        _principal_cache.invalidate(token)

    # -- Verification strategies ----------------------------------------------
    def _verify(self, token: str) -> dict[str, Any]:
        secret = self._settings.supabase_jwt_secret.get_secret_value()
        if secret:
            return self._verify_locally(token, secret)
        return self._verify_remotely(token)

    def _verify_locally(self, token: str, secret: str) -> dict[str, Any]:
        """Verify the HS256 signature, expiry and audience in-process.

        ``verify_aud`` is on. Skipping audience validation is a common shortcut
        and it is what lets a token minted for a different Supabase project — or
        for the ``anon`` role rather than an authenticated user — pass a signature
        check that looks correct.
        """
        try:
            claims = jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience=self._settings.supabase_jwt_audience,
                options={"verify_aud": True, "verify_exp": True, "require_exp": True},
            )
        except JWTError as exc:
            # The reason is logged, never returned: "signature invalid" versus
            # "token expired" tells an attacker which half of a forgery attempt
            # was wrong.
            logger.info("token rejected", extra={"reason": type(exc).__name__})
            raise AuthenticationError("Your session has expired. Sign in again.") from exc

        if not claims.get("sub"):
            raise AuthenticationError("Your session is not valid. Sign in again.")
        return claims

    def _verify_remotely(self, token: str) -> dict[str, Any]:
        """Verify by asking Supabase, and normalise the reply to claim shape.

        A ``401`` from Supabase surfaces as :class:`UpstreamError` from the
        gateway; it is translated here to an authentication failure so a bad token
        does not read as a server fault in the logs.
        """
        try:
            user = supabase.fetch_user(token)
        except UpstreamError as exc:
            raise AuthenticationError("Your session could not be verified.") from exc

        return {
            "sub": user.get("id"),
            "email": user.get("email"),
            "app_metadata": user.get("app_metadata") or {},
            "user_metadata": user.get("user_metadata") or {},
        }

    # -- Principal construction ------------------------------------------------
    def _build_principal(self, claims: dict[str, Any]) -> Principal:
        """Map verified claims onto a principal with resolved permissions.

        Authorization data comes from ``app_metadata``, never ``user_metadata``.
        The distinction is the whole authorization model: ``user_metadata`` is
        writable by the user's own session through the Supabase client, so a role
        read from there would let any signed-in user promote themselves. The
        monolith read ``app_metadata.get('role') or user_metadata.get('role')`` —
        the fallback made every account self-promotable to any role name it liked.
        """
        app_metadata = claims.get("app_metadata") or {}
        user_metadata = claims.get("user_metadata") or {}

        subject = as_text(claims.get("sub"))
        email = as_text(claims.get("email") or user_metadata.get("email"))
        display = as_text(
            user_metadata.get("display_name")
            or user_metadata.get("full_name")
            or user_metadata.get("name")
            or email
            or subject
        ) or "User"

        role_key = as_text(app_metadata.get("role")) or "tech"
        extra = parse_permissions(app_metadata.get("permissions"))

        role_permissions, rank, is_root = self._roles.resolve(role_key)
        granted = role_permissions | extra

        return Principal(
            subject=subject,
            username=email or subject,
            display=display,
            role_key=role_key,
            extra_permissions=extra,
            granted=granted,
            rank=rank,
            is_root=is_root,
            metadata={"provider": "supabase"},
        )


auth_service = AuthService()


def record_denial(principal: Principal | None, permission: str, path: str) -> None:
    """Audit a permission denial.

    Denials are the highest-signal security event a web application produces: a
    burst of them from one account is either a broken client or an account
    probing for what it can reach. The monolith logged none of them.
    """
    audit(
        "authz.denied",
        actor=principal.username if principal else "-",
        role=principal.role_key if principal else "-",
        permission=permission,
        path=path,
    )
