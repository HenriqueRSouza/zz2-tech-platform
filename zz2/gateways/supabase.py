"""Supabase gateway — authentication and REST.

Wraps the two ways this application talks to Supabase:

**Auth.** Verifying the access token a browser presents.

**REST (PostgREST).** Reading and writing ``techSupport`` tables with the service
key, and reading the read-only ``zoho`` customer-history schema.

The service key bypasses row-level security entirely. It is held in a
:class:`~pydantic.SecretStr`, injected per request, and never returned by any
method — the only path from this module to a browser is through an explicit
allowlist in :meth:`zz2.settings.Settings.public_config`.

Errors become :class:`~zz2.core.errors.UpstreamError`, carrying the upstream
detail in ``log_detail`` where it reaches the log but not the client. The monolith
returned ``'HTTP %d: %s' % (e.code, raw)`` with 500 bytes of the upstream body
straight to the caller, which leaked PostgREST messages naming tables, columns and
constraints.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

import httpx

from zz2.core.errors import NotConfigured, UpstreamError
from zz2.core.logging import logger
from zz2.gateways.http import get_client
from zz2.settings import get_settings

Method = Literal["GET", "POST", "PATCH", "DELETE"]


class SupabaseGateway:
    """Server-side Supabase access."""

    def __init__(self) -> None:
        self._settings = get_settings()

    # -- Guards ----------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self._settings.supabase_url)

    @property
    def service_configured(self) -> bool:
        return bool(
            self._settings.supabase_url
            and self._settings.supabase_service_key.get_secret_value()
        )

    def _require_service(self) -> str:
        key = self._settings.supabase_service_key.get_secret_value()
        if not (self._settings.supabase_url and key):
            raise NotConfigured("Supabase is not configured on this server.")
        return key

    # -- Auth ------------------------------------------------------------------
    def fetch_user(self, access_token: str) -> dict[str, Any]:
        """Resolve an access token to its user record via ``/auth/v1/user``.

        Used when no local JWT secret is configured, and as the authority when a
        token must be checked against Supabase's current state — a locally
        verified JWT stays valid until it expires even if the account was
        disabled a minute ago.

        Raises:
            NotConfigured: Supabase has no URL or publishable key.
            UpstreamError: The token was rejected or Supabase was unreachable.
        """
        if not (self._settings.supabase_url and self._settings.supabase_publishable_key):
            raise NotConfigured("Supabase authentication is not configured.")

        try:
            response = get_client().get(
                self._settings.supabase_auth_url + "user",
                headers={
                    "apikey": self._settings.supabase_publishable_key,
                    "Authorization": f"Bearer {access_token}",
                },
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(log_detail=f"supabase auth transport: {exc}") from exc

        if response.status_code != 200:
            raise UpstreamError(
                "Your session could not be verified.",
                log_detail=f"supabase auth HTTP {response.status_code}",
            )
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError(log_detail="supabase auth returned non-JSON") from exc

    # -- REST ------------------------------------------------------------------
    def request(
        self,
        method: Method,
        resource: str,
        *,
        payload: Any = None,
        prefer: str = "",
        schema: str | None = None,
    ) -> Any:
        """Perform one PostgREST request against ``resource``.

        Args:
            method: HTTP verb.
            resource: Table plus query string, e.g.
                ``"tickets?ticket_number=eq.TKT-1"``. Callers must percent-encode
                interpolated values with :func:`encode_value`.
            payload: JSON body for POST/PATCH.
            prefer: ``Prefer`` header, e.g.
                ``"resolution=merge-duplicates,return=minimal"``.
            schema: Postgres schema. Defaults to the application schema; pass
                the customer-history schema for read-only ``zoho`` tables.

        Returns:
            Parsed JSON, or ``None`` for an empty body.

        Raises:
            NotConfigured: No service key.
            UpstreamError: Transport failure or a non-2xx response.
        """
        key = self._require_service()
        target = schema or self._settings.supabase_schema
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept-Profile": target,
            "Content-Profile": target,
        }
        if prefer:
            headers["Prefer"] = prefer

        try:
            response = get_client().request(
                method,
                self._settings.supabase_rest_url + resource,
                json=payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(log_detail=f"supabase {method} {resource}: {exc}") from exc

        if response.status_code >= 400:
            # The upstream body often names tables and constraints. It goes to
            # the log; the client gets the generic message.
            raise UpstreamError(
                log_detail=(
                    f"supabase {method} {resource.split('?')[0]} "
                    f"HTTP {response.status_code}: {response.text[:500]}"
                )
            )

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            logger.warning("supabase returned non-JSON",
                           extra={"resource": resource.split("?")[0]})
            return None

    # -- Convenience -----------------------------------------------------------
    def select(self, resource: str, *, schema: str | None = None) -> list[dict[str, Any]]:
        """GET returning a row list (empty when the response is not a list)."""
        rows = self.request("GET", resource, schema=schema)
        return rows if isinstance(rows, list) else []

    def select_paged(
        self,
        resource_template: str,
        *,
        page_size: int = 1000,
        max_pages: int = 50,
        schema: str | None = None,
    ) -> list[dict[str, Any]]:
        """Page through a result set.

        PostgREST caps a response at 1,000 rows by default, and a query that hits
        the cap returns a *successful* truncated result — indistinguishable from a
        complete one. Paging explicitly is the only way to know the set is whole.

        Args:
            resource_template: Resource containing ``{limit}`` and ``{offset}``
                placeholders.
            page_size: Rows per request.
            max_pages: Safety bound, so a mistaken template cannot loop forever.
        """
        rows: list[dict[str, Any]] = []
        for page in range(max_pages):
            batch = self.select(
                resource_template.format(limit=page_size, offset=page * page_size),
                schema=schema,
            )
            rows.extend(batch)
            if len(batch) < page_size:
                return rows
        logger.warning("paged query hit the page limit; result may be truncated",
                       extra={"resource": resource_template.split("?")[0],
                              "pages": max_pages, "rows": len(rows)})
        return rows

    def upsert(self, table: str, rows: list[dict[str, Any]], *, on_conflict: str) -> None:
        """Merge rows on a conflict target, returning no representation."""
        if not rows:
            return
        self.request(
            "POST",
            f"{table}?on_conflict={on_conflict}",
            payload=rows,
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def delete_where(self, table: str, filter_query: str) -> None:
        self.request("DELETE", f"{table}?{filter_query}", prefer="return=minimal")


def encode_value(value: object) -> str:
    """Percent-encode a value for a PostgREST filter.

    Mandatory for anything client-supplied. A raw customer name containing ``&``
    or ``,`` would otherwise be read as PostgREST filter syntax — the query-string
    equivalent of an injection, and the reason the monolith's
    ``'customer_name=eq.' + enc`` construction is only safe where ``enc`` came
    from :func:`urllib.parse.quote`.
    """
    return quote(str(value or ""), safe="")


supabase = SupabaseGateway()


class SupabaseAdminGateway:
    """Supabase Auth Admin API — the user directory.

    Replaces ``credentials.json``. The monolith stored users in a JSON file on the
    NAS with hashes it computed itself; Supabase now owns identities, passwords and
    password policy, and this gateway is how the Users page reaches them.

    Every call here uses the service key and can create or delete any account, so
    every caller must already have passed a ``manage_users`` check *and* the rank
    comparison in :mod:`zz2.domain.permissions`.

    Application authorization lives in ``app_metadata``, never ``user_metadata``.
    A user's own session can write ``user_metadata`` through the Supabase client;
    if a role were read from there, any signed-in user could promote themselves.
    """

    PERMANENT_BAN = "876000h"
    """Effectively forever — a hundred years. Supabase has no "disabled" flag, so a
    deactivated account is a banned one, and the ban is what makes it reversible."""

    def __init__(self) -> None:
        self._settings = get_settings()

    def _headers(self) -> dict[str, str]:
        key = self._settings.supabase_service_key.get_secret_value()
        if not (self._settings.supabase_url and key):
            raise NotConfigured("Supabase user management is not configured.")
        return {"apikey": key, "Authorization": f"Bearer {key}",
                "Content-Type": "application/json"}

    def _request(self, method: Method, path: str, payload: Any = None) -> Any:
        try:
            response = get_client().request(
                method,
                self._settings.supabase_auth_url + path,
                json=payload,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(log_detail=f"supabase admin {method} {path}: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text[:500]
            # Supabase reports "already registered" as a 422. Surfacing it as a
            # generic upstream failure would leave the operator guessing why a
            # perfectly valid-looking account could not be created.
            if response.status_code in (409, 422) and "already" in detail.lower():
                raise UpstreamError(
                    "An account with that email address already exists.",
                    log_detail=f"supabase admin {path}: {detail}",
                )
            raise UpstreamError(
                log_detail=f"supabase admin {method} {path} "
                           f"HTTP {response.status_code}: {detail}"
            )
        return response.json() if response.content else None

    def list_users(self, *, per_page: int = 200, max_pages: int = 25) -> list[dict[str, Any]]:
        """Every account, paged.

        The admin endpoint pages at 50 by default and silently stops there, so an
        unpaged call would show only the first page of a growing team and the
        missing users would look deleted.
        """
        users: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            body = self._request("GET", f"admin/users?page={page}&per_page={per_page}")
            batch = (body or {}).get("users") or []
            users.extend(batch)
            if len(batch) < per_page:
                break
        return users

    def create_user(
        self,
        *,
        email: str,
        password: str,
        display: str,
        role: str,
        permissions: list[str],
    ) -> dict[str, Any]:
        """Create a confirmed account.

        ``email_confirm=True`` because these are staff accounts created by an
        administrator — there is no inbox to click a link in, and an unconfirmed
        account cannot sign in.
        """
        return self._request("POST", "admin/users", {
            "email": email,
            "password": password,
            "email_confirm": True,
            "app_metadata": {"role": role, "permissions": permissions},
            "user_metadata": {"display_name": display},
        }) or {}

    def update_user(self, user_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"admin/users/{user_id}", changes) or {}

    def set_active(self, user_id: str, *, active: bool) -> dict[str, Any]:
        """Enable or disable sign-in for an account."""
        return self.update_user(
            user_id, {"ban_duration": "none" if active else self.PERMANENT_BAN}
        )

    def set_password(self, user_id: str, password: str) -> dict[str, Any]:
        return self.update_user(user_id, {"password": password})

    def set_authorization(
        self, user_id: str, *, role: str, permissions: list[str]
    ) -> dict[str, Any]:
        """Replace an account's role and extra permissions.

        A whole-object write: Supabase replaces ``app_metadata`` rather than
        merging it, so both fields must always be sent together. Sending only the
        role would silently clear every extra grant.
        """
        return self.update_user(
            user_id, {"app_metadata": {"role": role, "permissions": permissions}}
        )

    def delete_user(self, user_id: str) -> None:
        self._request("DELETE", f"admin/users/{user_id}")


supabase_admin = SupabaseAdminGateway()
