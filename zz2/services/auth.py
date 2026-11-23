"""Authentication: access token to :class:`Principal`.

Every session is one this server issued (:mod:`zz2.services.session_tokens`):
after a password sign-in against ``app_users``, a Microsoft (OpenID Connect)
sign-in, or a SAML sign-in. Tokens are HS256 JWTs verified in-process — no
network round-trip per request.

Authorization is re-read from the user directory on each (uncached) request when
the token belongs to an account, so a role change or a deactivation takes effect
within :data:`PRINCIPAL_CACHE_TTL` seconds instead of at token expiry. If the
database is briefly unreachable the role snapshot inside the token is used.

Tokens are read from the ``Authorization`` header **only**, never the query
string, which would write live tokens into logs, ``Referer`` headers and history.
"""

from __future__ import annotations

import uuid
from typing import Any

from zz2.core.cache import TTLCache
from zz2.core.errors import AuthenticationError, NotConfigured, UpstreamError
from zz2.core.logging import audit, logger
from zz2.core.text import as_text
from zz2.domain.permissions import Principal, parse_permissions
from zz2.gateways.user_directory import user_directory
from zz2.repositories.roles import RoleRepository
from zz2.services import session_tokens
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

    @staticmethod
    def sign_out(token: str) -> bool:
        """End a session server-side.

        The token is revoked outright, so a copied token stops working at once
        rather than at expiry. Returns whether the token was revoked.
        """
        _principal_cache.invalidate(token)
        return session_tokens.revoke(token) if session_tokens.is_session_token(token) else False

    # -- Verification ------------------------------------------------------------
    @staticmethod
    def _verify(token: str) -> dict[str, Any]:
        if not session_tokens.is_session_token(token):
            raise AuthenticationError("Your session is not valid. Sign in again.")
        claims = session_tokens.verify(token)
        if not claims.get("sub"):
            raise AuthenticationError("Your session is not valid. Sign in again.")
        return claims

    @staticmethod
    def _current_account(subject: str) -> dict[str, Any] | None:
        """The live account behind ``subject``, or ``None`` when the token is not
        tied to one (or the directory is unavailable).

        Raises:
            AuthenticationError: The account was deleted or deactivated.
        """
        if not _looks_like_uuid(subject) or not user_directory.configured:
            return None
        try:
            account = user_directory.get_user(subject)
        except (NotConfigured, UpstreamError):
            logger.warning("user directory unavailable; using token role snapshot")
            return None
        if account is None or account.get("banned_until"):
            raise AuthenticationError("Your account is not active. Sign in again.")
        return account

    # -- Principal construction ------------------------------------------------
    def _build_principal(self, claims: dict[str, Any]) -> Principal:
        """Map verified claims onto a principal with resolved permissions.

        Authorization data comes from ``app_metadata`` (the account's ``role`` and
        ``permissions`` columns), never ``user_metadata``. The monolith read
        ``app_metadata.get('role') or user_metadata.get('role')`` — the fallback
        made every account self-promotable to any role name it liked.
        """
        subject = as_text(claims.get("sub"))
        provider = as_text((claims.get("app_metadata") or {}).get("provider")) or "password"
        account = self._current_account(subject)
        source = account if account is not None else claims
        app_metadata = source.get("app_metadata") or {}
        user_metadata = source.get("user_metadata") or {}

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
            metadata={"provider": provider},
        )


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


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
