"""Sign-in endpoints: password and Microsoft (OpenID Connect).

POST /api/auth/login                 Email + password against ``app_users``
GET  /api/auth/microsoft/login       Start "Entrar com Microsoft"
GET  /api/auth/microsoft/callback    Microsoft returns here with an auth code
POST /api/auth/sso/exchange          The sign-in page trades its one-time code

SAML keeps its own routes in :mod:`zz2.api.routers.saml`; both SSO flows end with
the same ``/login.html#sso=<code>`` hand-off and the same exchange.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from zz2.api.middleware.auth_guard import client_address
from zz2.api.routers.saml import DEFAULT_NEXT, safe_next_path
from zz2.core.errors import AuthenticationError, NotFound
from zz2.core.logging import audit
from zz2.gateways.user_directory import user_directory
from zz2.services import session_tokens
from zz2.services.lockout import fingerprint, lockout_guard
from zz2.services.microsoft import MicrosoftLoginError, microsoft_signin
from zz2.services.saml import SamlError
from zz2.services.sso import SsoDenied, sso_service
from zz2.settings import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])

MS_STATE_COOKIE = "zz2_ms_state"
MS_COOKIE_PATH = "/api/auth/microsoft"


# -- Password ------------------------------------------------------------------
class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


@router.post("/login")
def password_login(payload: LoginRequest, request: Request) -> dict[str, Any]:
    address = client_address(request)
    account = user_directory.authenticate(payload.email, payload.password)
    if account is None:
        lockout_guard.record_failure(address, fingerprint(f"{payload.email}:{payload.password}"),
                                     source="password")
        audit("password.rejected", client=address, actor=payload.email.lower())
        raise AuthenticationError("Email ou senha inválidos.")

    meta = account.get("app_metadata") or {}
    display = (account.get("user_metadata") or {}).get("display_name") or account["email"]
    role = meta.get("role") or "tech"
    token = session_tokens.issue(
        subject=account["id"], email=account["email"], display=display,
        role=role, permissions=list(meta.get("permissions") or []), provider="password",
    )
    audit("password.login", actor=account["email"], role=role, client=address)
    return {
        "ok": True,
        "token": token,
        "expires_in": get_settings().session_ttl_seconds,
        "user": {"email": account["email"], "display": display, "role": role},
    }


# -- Microsoft -------------------------------------------------------------------
def _require_microsoft() -> None:
    if not get_settings().microsoft_enabled:
        raise NotFound()


def _back_to_login(fragment: str) -> RedirectResponse:
    response = RedirectResponse(f"/login.html#{fragment}", status_code=303)
    response.delete_cookie(MS_STATE_COOKIE, path=MS_COOKIE_PATH, secure=True,
                           httponly=True, samesite="lax")
    return response


@router.get("/microsoft/login")
def microsoft_login(
    next: Annotated[str, Query(max_length=200)] = DEFAULT_NEXT,  # noqa: A002
) -> RedirectResponse:
    _require_microsoft()
    start = microsoft_signin.start_login(safe_next_path(next))
    response = RedirectResponse(start.redirect_url, status_code=302)
    # Lax is enough: Microsoft returns with a top-level GET redirect.
    response.set_cookie(MS_STATE_COOKIE, start.browser_nonce, max_age=600,
                        path=MS_COOKIE_PATH, secure=True, httponly=True, samesite="lax")
    return response


@router.get("/microsoft/callback", include_in_schema=False)
def microsoft_callback(
    request: Request,
    code: Annotated[str, Query(max_length=4096)] = "",
    state: Annotated[str, Query(max_length=200)] = "",
    error: Annotated[str, Query(max_length=200)] = "",
) -> RedirectResponse:
    _require_microsoft()
    address = client_address(request)
    if error:
        # The user cancelled, or the tenant refused consent.
        audit("microsoft.cancelled", client=address, reason=error)
        return _back_to_login("sso_error=cancelled")
    try:
        identity, next_path = microsoft_signin.complete(
            code=code, state=state, browser_nonce=request.cookies.get(MS_STATE_COOKIE))
        sso_code = sso_service.complete(identity, next_path, provider="microsoft")
    except (MicrosoftLoginError, SamlError) as exc:
        audit("microsoft.rejected", client=address, reason=getattr(exc, "reason", "-"))
        if code:
            lockout_guard.record_failure(address, fingerprint(code), source="microsoft")
        return _back_to_login("sso_error=rejected")
    except SsoDenied as exc:
        audit("microsoft.denied", client=address, reason=exc.reason)
        return _back_to_login("sso_error=denied")
    return _back_to_login(f"sso={sso_code}")


# -- Shared exchange ---------------------------------------------------------------
class ExchangeRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    code: str = Field(min_length=20, max_length=100)


@router.post("/sso/exchange")
def sso_exchange(payload: ExchangeRequest, request: Request) -> dict[str, Any]:
    settings = get_settings()
    if not (settings.microsoft_enabled or settings.saml_enabled):
        raise NotFound()
    issued = sso_service.exchange(payload.code)
    if issued is None:
        lockout_guard.record_failure(client_address(request), fingerprint(payload.code),
                                     source="sso_code")
        raise AuthenticationError("Este link de acesso expirou. Entre novamente.")
    return {"ok": True, "token": issued.token, "next": issued.next_path}
