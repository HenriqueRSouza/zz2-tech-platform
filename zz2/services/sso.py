"""Single sign-on: from a verified SAML identity to a signed-in user.

:mod:`zz2.services.saml` proves *who* the user is. This module decides what they
may do, and hands the browser a session.

Authorization, in order:

1. **Domain allowlist.** ``ZZ2_SAML_ALLOWED_DOMAINS`` — only university addresses.
2. **An existing account wins.** If an app_users account has the same email, its
   ``app_metadata`` role and grants apply, and a *deactivated* account is refused.
   Without this, deactivating someone in the Users page would not stop them
   walking back in through Microsoft.
3. **Group mapping.** ``ZZ2_SAML_GROUP_ROLE_MAP`` maps Entra group object IDs to
   roles; the highest-ranked match wins.
4. **Default role.** ``ZZ2_SAML_DEFAULT_ROLE``, ``tech`` unless configured.

If the user directory is configured but unreachable the sign-in is refused rather
than falling through to the default role — failing open there would let a
deactivated account in whenever the database is slow.

Handing over the session: the ACS endpoint is a cross-site POST from Microsoft,
so it cannot simply return a token to page JavaScript. It redirects to
``/login.html#sso=<code>`` — a fragment, which never reaches a server log or a
``Referer`` — and the page exchanges the single-use, 60-second code for the token.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from zz2.core.cache import TTLCache
from zz2.core.errors import AppError, NotConfigured, UpstreamError
from zz2.core.logging import audit, logger
from zz2.core.text import as_text
from zz2.gateways.user_directory import user_directory
from zz2.repositories.roles import RoleRepository
from zz2.services import session_tokens
from zz2.services.saml import SamlError, SamlIdentity
from zz2.settings import get_settings

CODE_TTL_SECONDS = 60.0


class SsoDenied(AppError):
    """A valid identity that is not allowed in."""

    status_code = 403
    code = "sso_denied"

    def __init__(self, reason: str) -> None:
        super().__init__("This account is not allowed to sign in.", log_detail=reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class IssuedSession:
    token: str
    next_path: str


class SsoService:
    def __init__(self, roles: RoleRepository | None = None) -> None:
        self._roles = roles or RoleRepository()
        self._codes: TTLCache[str, IssuedSession] = TTLCache(CODE_TTL_SECONDS, 2000)

    def reset(self) -> None:
        self._codes.clear()

    # -- Sign-in ---------------------------------------------------------------
    def complete(self, identity: SamlIdentity, next_path: str, *,
                 provider: str = "saml") -> str:
        """Authorize ``identity`` and return a one-time code for the browser.

        ``provider`` is ``"saml"`` or ``"microsoft"`` (OpenID Connect). Both carry
        the same identity shape and follow the same authorization rules.

        Raises:
            SsoDenied: The identity is valid but not allowed in.
            SamlError: The identity carries no usable email.
        """
        settings = get_settings()
        email = identity.email.strip().lower()
        if not email or "@" not in email:
            raise SamlError("no_email_claim")
        domain = email.rsplit("@", 1)[1]
        if settings.saml_allowed_domains and domain not in settings.saml_allowed_domains:
            raise SsoDenied("domain_not_allowed")

        role, permissions, display, account_id = self._authorization(identity)
        subject = account_id or f"{provider}:{identity.object_id or identity.name_id or email}"
        token = session_tokens.issue(
            subject=subject, email=email, display=display or email,
            role=role, permissions=permissions, provider=provider,
            extra={"tenant_id": identity.tenant_id} if identity.tenant_id else None,
        )
        code = secrets.token_urlsafe(32)
        self._codes.set(code, IssuedSession(token, next_path))
        audit(f"{provider}.login", actor=email, role=role, linked_account=bool(account_id),
              tenant=identity.tenant_id or "-")
        return code

    def exchange(self, code: str) -> IssuedSession | None:
        """Redeem a code exactly once."""
        issued = self._codes.get(code)
        self._codes.invalidate(code)
        return issued

    # -- Authorization ---------------------------------------------------------
    def _authorization(self, identity: SamlIdentity) -> tuple[str, list[str], str, str]:
        """``(role, extra_permissions, display, account_id)``.

        An existing account (matched by email) supplies role and grants and is
        linked to the Microsoft object ID. A first-time user gets an account
        created with no password — so administrators see and manage them on the
        Users page like anyone else — with the role from group mapping.
        """
        account = self._existing_account(identity.email)
        if account is not None:
            if account.get("banned_until"):
                raise SsoDenied("account_deactivated")
            app_metadata = account.get("app_metadata") or {}
            user_metadata = account.get("user_metadata") or {}
            permissions = app_metadata.get("permissions") or []
            account_id = as_text(account.get("id"))
            self._touch(account_id, identity.object_id)
            return (
                as_text(app_metadata.get("role")) or get_settings().saml_default_role,
                [as_text(p) for p in permissions] if isinstance(permissions, list) else [],
                as_text(user_metadata.get("display_name")) or identity.display_name,
                account_id,
            )
        role = self._role_from_groups(identity.groups)
        account_id = self._provision(identity, role)
        return role, [], identity.display_name, account_id

    @staticmethod
    def _existing_account(email: str) -> dict | None:
        try:
            return user_directory.find_by_email(email)
        except NotConfigured:
            return None
        except UpstreamError as exc:
            logger.warning("user directory unreachable during SSO sign-in")
            raise SsoDenied("directory_unavailable") from exc

    @staticmethod
    def _touch(account_id: str, object_id: str) -> None:
        try:
            user_directory.touch_sign_in(account_id, microsoft_oid=object_id or None)
        except (NotConfigured, UpstreamError):
            logger.warning("could not record SSO sign-in time")

    @staticmethod
    def _provision(identity: SamlIdentity, role: str) -> str:
        try:
            account = user_directory.create_user(
                email=identity.email, password=None,
                display=identity.display_name or identity.email, role=role,
                permissions=[], microsoft_oid=identity.object_id or None,
            )
        except NotConfigured:
            return ""
        except UpstreamError as exc:
            logger.warning("could not provision SSO account")
            raise SsoDenied("directory_unavailable") from exc
        account_id = as_text(account.get("id"))
        if account_id:
            user_directory.touch_sign_in(account_id)
        return account_id

    def _role_from_groups(self, groups: tuple[str, ...]) -> str:
        settings = get_settings()
        mapping: dict[str, str] = {}
        for pair in settings.saml_group_role_map:
            group, _, role = pair.partition("=")
            if group.strip() and role.strip():
                mapping[group.strip().lower()] = role.strip()
        matched = [mapping[g.lower()] for g in groups if g.lower() in mapping]
        if not matched:
            return settings.saml_default_role
        return max(matched, key=lambda role: self._roles.resolve(role)[1])


sso_service = SsoService()
