"""SAML sign-in with Microsoft Entra ID (university accounts).

=======  ===========================  ==============================================
Method   Path                         Purpose
=======  ===========================  ==============================================
GET      /api/auth/saml/metadata      SP metadata to upload into Entra ID
GET      /api/auth/saml/login         Start: redirect to Microsoft
POST     /api/auth/saml/acs           Microsoft posts the signed response here
POST     /api/auth/saml/exchange      The sign-in page trades its code for a token
=======  ===========================  ==============================================

All four answer ``404`` while ``ZZ2_SAML_ENABLED`` is off, so a deployment that
does not use SAML exposes no SAML surface at all.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from zz2.api.middleware.auth_guard import client_address
from zz2.core.errors import AuthenticationError, NotFound
from zz2.core.logging import audit
from zz2.services.lockout import fingerprint, lockout_guard
from zz2.services.saml import MAX_RESPONSE_BYTES, SamlError, saml_sp
from zz2.services.sso import SsoDenied, sso_service
from zz2.settings import get_settings

router = APIRouter(prefix="/api/auth/saml", tags=["auth"])

STATE_COOKIE = "zz2_saml_state"
COOKIE_PATH = "/api/auth/saml"
DEFAULT_NEXT = "/dashboard.html"
_SAFE_NEXT = re.compile(r"^/[A-Za-z0-9_\-./]*(\?[A-Za-z0-9_\-=&%.]*)?$")


def _require_enabled() -> None:
    if not get_settings().saml_enabled:
        raise NotFound()


def safe_next_path(value: str | None) -> str:
    """Only same-site paths. ``//evil.example`` and ``/\\evil`` are open redirects."""
    if not value or value.startswith("//") or "\\" in value or not _SAFE_NEXT.match(value):
        return DEFAULT_NEXT
    return value


def _back_to_login(fragment: str) -> RedirectResponse:
    response = RedirectResponse(f"/login.html#{fragment}", status_code=303)
    response.delete_cookie(STATE_COOKIE, path=COOKIE_PATH, secure=True,
                           httponly=True, samesite="none")
    return response


@router.get("/metadata", include_in_schema=False)
def metadata() -> Response:
    _require_enabled()
    return Response(saml_sp.metadata_xml(), media_type="application/samlmetadata+xml")


@router.get("/login")
def login(next: Annotated[str, Query(max_length=200)] = DEFAULT_NEXT) -> RedirectResponse:  # noqa: A002
    _require_enabled()
    start = saml_sp.start_login(safe_next_path(next))
    response = RedirectResponse(start.redirect_url, status_code=302)
    # SameSite=None is required, not lax: Microsoft returns the user with a
    # cross-site POST, and a Lax cookie is not sent on one. Secure is mandatory
    # with None — browsers treat http://localhost as secure for development.
    response.set_cookie(STATE_COOKIE, start.browser_nonce, max_age=600, path=COOKIE_PATH,
                        secure=True, httponly=True, samesite="none")
    return response


@router.post("/acs", include_in_schema=False)
def assertion_consumer(
    request: Request,
    saml_response: Annotated[str, Form(alias="SAMLResponse",
                                       max_length=MAX_RESPONSE_BYTES * 2)] = "",
) -> RedirectResponse:
    _require_enabled()
    address = client_address(request)
    try:
        identity, next_path = saml_sp.consume(saml_response,
                                              request.cookies.get(STATE_COOKIE))
        code = sso_service.complete(identity, next_path)
    except SamlError as exc:
        audit("saml.rejected", client=address, reason=exc.reason)
        # Counted towards the address's lockout: forging responses is exactly
        # the kind of repeated attempt the guard exists for.
        if saml_response:
            lockout_guard.record_failure(address, fingerprint(saml_response), source="saml")
        return _back_to_login("sso_error=rejected")
    except SsoDenied as exc:
        audit("saml.denied", client=address, reason=exc.reason)
        return _back_to_login("sso_error=denied")
    return _back_to_login(f"sso={code}")


class ExchangeRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    code: str = Field(min_length=20, max_length=100)


@router.post("/exchange")
def exchange(payload: ExchangeRequest, request: Request) -> dict[str, Any]:
    _require_enabled()
    issued = sso_service.exchange(payload.code)
    if issued is None:
        lockout_guard.record_failure(client_address(request), fingerprint(payload.code),
                                     source="saml_code")
        raise AuthenticationError("This sign-in link has expired. Sign in again.")
    return {"ok": True, "token": issued.token, "next": issued.next_path}
