"""Sign in with Microsoft (Outlook / Microsoft 365) through OpenID Connect.

Authorization-code flow with PKCE against the Microsoft identity platform v2.0
endpoints, run entirely server-side with a confidential client (client secret):

```
Browser                 TSP                                   login.microsoftonline.com
  │ GET /api/auth/microsoft/login ─▶ state, nonce, PKCE; cookie = browser nonce
  │ ◀── 302 ─────────────────────── /authorize?…&code_challenge=…
  │ ─────────────────────────────────────────────────────────▶ sign in (+ MFA)
  │ ◀──────────────────────────────── 302 /api/auth/microsoft/callback?code&state
  │ GET /callback ─▶ check state + cookie; POST /token (code, verifier, secret)
  │                  verify id_token: RS256 via JWKS, aud, iss↔tid, exp, nonce
  │ ◀── 303 /login.html#sso=<one-time code>   (same hand-off as SAML)
```

The identity is handed to :mod:`zz2.services.sso`, which applies the same rules
as SAML: domain allowlist, existing account wins (and a deactivated one is
refused), group mapping, default role, auto-provisioned account.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from jose import JWTError, jwt

from zz2.core.cache import TTLCache
from zz2.core.logging import logger
from zz2.core.text import as_text
from zz2.gateways.http import get_client
from zz2.services.saml import SamlIdentity
from zz2.settings import get_settings

AUTHORITY = "https://login.microsoftonline.com"
SCOPES = "openid profile email"
STATE_TTL_SECONDS = 600.0
JWKS_TTL_SECONDS = 6 * 3600.0
_GUID_LEN = 36


class MicrosoftLoginError(Exception):
    """The sign-in could not be completed. ``reason`` is logged, not shown."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class LoginStart:
    redirect_url: str
    browser_nonce: str


@dataclass(frozen=True, slots=True)
class _Pending:
    browser_hash: str
    nonce: str
    verifier: str
    next_path: str


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class MicrosoftSignIn:
    def __init__(self) -> None:
        self._pending: TTLCache[str, _Pending] = TTLCache(STATE_TTL_SECONDS, 5000)
        self._jwks: dict[str, Any] = {}
        self._jwks_at = 0.0

    def reset(self) -> None:
        self._pending.clear()
        self._jwks, self._jwks_at = {}, 0.0

    @property
    def _tenant(self) -> str:
        return get_settings().microsoft_tenant.strip() or "organizations"

    # -- Step 1 ----------------------------------------------------------------
    def start_login(self, next_path: str) -> LoginStart:
        settings = get_settings()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        browser_nonce = secrets.token_urlsafe(32)
        self._pending.set(state, _Pending(_sha256(browser_nonce), nonce, verifier, next_path))
        query = urlencode({
            "client_id": settings.microsoft_client_id,
            "response_type": "code",
            "redirect_uri": settings.microsoft_redirect_uri,
            "response_mode": "query",
            "scope": SCOPES,
            "state": state,
            "nonce": nonce,
            "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
            "code_challenge_method": "S256",
            "prompt": "select_account",
        })
        return LoginStart(f"{AUTHORITY}/{self._tenant}/oauth2/v2.0/authorize?{query}",
                          browser_nonce)

    # -- Step 2 ----------------------------------------------------------------
    def complete(self, *, code: str, state: str,
                 browser_nonce: str | None) -> tuple[SamlIdentity, str]:
        """Redeem the authorization code and return ``(identity, next_path)``."""
        pending = self._pending.get(state) if state else None
        if state:
            self._pending.invalidate(state)  # single use, whatever happens next
        if pending is None:
            raise MicrosoftLoginError("unknown_state")
        if not browser_nonce or not secrets.compare_digest(_sha256(browser_nonce),
                                                           pending.browser_hash):
            raise MicrosoftLoginError("browser_mismatch")
        if not code:
            raise MicrosoftLoginError("no_code")

        id_token = self._redeem(code, pending.verifier)
        claims = self._verify_id_token(id_token, pending.nonce)
        return self._identity(claims), pending.next_path

    def _redeem(self, code: str, verifier: str) -> str:
        settings = get_settings()
        try:
            response = get_client().post(
                f"{AUTHORITY}/{self._tenant}/oauth2/v2.0/token",
                data={
                    "client_id": settings.microsoft_client_id,
                    "client_secret": settings.microsoft_client_secret.get_secret_value(),
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.microsoft_redirect_uri,
                    "code_verifier": verifier,
                    "scope": SCOPES,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise MicrosoftLoginError(f"token_transport: {type(exc).__name__}") from exc
        if response.status_code != 200:
            error = ""
            try:
                error = as_text(response.json().get("error"))
            except ValueError:
                pass
            raise MicrosoftLoginError(f"token_http_{response.status_code}:{error}")
        token = as_text(response.json().get("id_token"))
        if not token:
            raise MicrosoftLoginError("no_id_token")
        return token

    def _signing_keys(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._jwks and not refresh and time.monotonic() - self._jwks_at < JWKS_TTL_SECONDS:
            return self._jwks
        try:
            response = get_client().get(f"{AUTHORITY}/{self._tenant}/discovery/v2.0/keys")
            response.raise_for_status()
            keys = {k["kid"]: k for k in response.json().get("keys", []) if k.get("kid")}
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            if self._jwks:
                logger.warning("could not refresh Microsoft signing keys; using cached set")
                return self._jwks
            raise MicrosoftLoginError("jwks_unavailable") from exc
        self._jwks, self._jwks_at = keys, time.monotonic()
        return keys

    def _verify_id_token(self, token: str, nonce: str) -> dict[str, Any]:
        settings = get_settings()
        try:
            header = jwt.get_unverified_header(token)
        except JWTError as exc:
            raise MicrosoftLoginError("id_token_malformed") from exc
        kid = as_text(header.get("kid"))
        key = self._signing_keys().get(kid) or self._signing_keys(refresh=True).get(kid)
        if key is None:
            raise MicrosoftLoginError("unknown_signing_key")
        try:
            claims = jwt.decode(
                token, key, algorithms=["RS256"], audience=settings.microsoft_client_id,
                options={"verify_at_hash": False, "require_exp": True, "require_iat": True},
            )
        except JWTError as exc:
            raise MicrosoftLoginError(f"id_token_invalid: {type(exc).__name__}") from exc

        # Multi-tenant authorities ("organizations", "common") sign tokens whose
        # issuer names the user's own tenant, so the issuer is checked against the
        # token's tid — and against the configured tenant when it is a GUID.
        tid = as_text(claims.get("tid"))
        if not tid or claims.get("iss") != f"{AUTHORITY}/{tid}/v2.0":
            raise MicrosoftLoginError("issuer_mismatch")
        tenant = self._tenant
        if len(tenant) == _GUID_LEN and tenant.lower() != tid.lower():
            raise MicrosoftLoginError("tenant_mismatch")
        if not secrets.compare_digest(as_text(claims.get("nonce")), nonce):
            raise MicrosoftLoginError("nonce_mismatch")
        return claims

    @staticmethod
    def _identity(claims: dict[str, Any]) -> SamlIdentity:
        email = as_text(claims.get("email") or claims.get("preferred_username")
                        or claims.get("upn")).strip().lower()
        groups = claims.get("groups") or []
        return SamlIdentity(
            name_id=as_text(claims.get("sub")),
            email=email,
            display_name=as_text(claims.get("name")) or email,
            object_id=as_text(claims.get("oid")),
            tenant_id=as_text(claims.get("tid")),
            groups=tuple(as_text(g) for g in groups) if isinstance(groups, list) else (),
        )


microsoft_signin = MicrosoftSignIn()
