"""Compatibility shims for the monolith's URL scheme.

Every route here is deprecated and exists for one reason: to make the cutover
safe. A browser holding a cached page from the old server, or a page whose
JavaScript has not been migrated yet, keeps working instead of failing on its
first request.

Each shim delegates to the canonical handler — no logic is duplicated, so the
authorization and validation are identical. They are hidden from the OpenAPI
schema so the documented API is the new one, and every call is logged at
``deprecated_route`` so the access log shows exactly when the last caller stops
using them and they can be deleted.

The one genuinely new behaviour is what these routes *refuse*. The monolith's
``POST /verify`` took the session token in the request body and ``GET`` requests
accepted ``?token=``; neither is honoured here. A caller sending credentials that
way gets a 401 and must move them to the ``Authorization`` header.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, RedirectResponse

from zz2.api.deps import (
    CurrentPrincipal,
    DeadLetters,
    PendingWrites,
    RequireCreateTickets,
    RequireKitEditor,
    RequireManageRoles,
    RequireManageUsers,
    RequireSystem,
    RequireTickets,
    TicketStore,
)
from zz2.api.routers import analytics as analytics_router
from zz2.api.routers import kits as kits_router
from zz2.api.routers import meta as meta_router
from zz2.api.routers import roles as roles_router
from zz2.api.routers import system as system_router
from zz2.api.routers import tickets as tickets_router
from zz2.core.errors import NotFound, ValidationError
from zz2.core.logging import logger
from zz2.domain.permissions import Principal
from zz2.services.catalog import catalog_service
from zz2.services.kits import kit_service
from zz2.services.roles import role_service
from zz2.services.telemetry import telemetry_service
from zz2.services.tickets import ticket_service
from zz2.services.users import user_service

router = APIRouter(include_in_schema=False, tags=["compat"])


def _deprecated(request: Request, replacement: str) -> None:
    """Record one call to a legacy route."""
    logger.info("deprecated_route",
                extra={"path": request.url.path, "replacement": replacement})


# -- Session -------------------------------------------------------------------
@router.post("/verify")
def legacy_verify(
    request: Request,
    actor: Annotated[Principal, CurrentPrincipal],
) -> dict[str, Any]:
    """Old session check.

    The token must arrive in the ``Authorization`` header. A body-supplied token
    is ignored, so a page that has not been migrated fails closed with a 401 and
    redirects to sign-in rather than silently continuing to leak the token into
    request bodies.
    """
    _deprecated(request, "POST /api/session/verify")
    return meta_router.verify_session(actor)


@router.post("/logout")
def legacy_logout(request: Request) -> dict[str, Any]:
    """Old sign-out. Unauthenticated: it has nothing left to do server-side."""
    _deprecated(request, "POST /api/session/logout")
    return {"ok": True}


@router.post("/login")
def legacy_login(request: Request) -> dict[str, Any]:
    """Old username/password sign-in. Permanently gone.

    Answers 410 with an explanation rather than 404, so an old cached page shows
    the operator something actionable instead of a blank failure.
    """
    _deprecated(request, "POST /api/auth/login")
    raise ValidationError(
        "Password sign-in has moved to the new sign-in page. Reload the sign-in page.",
        detail={"gone": True},
    )


# -- Tickets -------------------------------------------------------------------
@router.get("/api/tickets")
def legacy_ticket_store(
    request: Request,
    actor: Annotated[Principal, RequireTickets],
    store: TicketStore,
) -> dict[str, Any]:
    _deprecated(request, "GET /api/analytics/store")
    return analytics_router.ticket_store(actor, store)


@router.get("/api/tickets/get")
def legacy_ticket_get(
    request: Request,
    actor: Annotated[Principal, CurrentPrincipal],
    ticketNum: Annotated[str, Query(alias="ticketNum")] = "",  # noqa: N803
) -> dict[str, Any]:
    _deprecated(request, "GET /api/tickets/{number}")
    result = ticket_service.get(ticketNum)
    if result is None:
        raise NotFound("Ticket not found.")
    return {"ok": True, "result": result}


@router.get("/api/proxy")
def legacy_proxy_get(
    request: Request,
    actor: Annotated[Principal, CurrentPrincipal],
    action: str = "",
    sheet: str = "",
) -> dict[str, Any]:
    """Old GET proxy.

    Only ``action=csv`` is honoured, and it now serves the local catalogue — which
    is what the monolith's own comment said it always did ("never fall back to a
    remote CSV"). The open-ended Apps Script pass-through is *not* reimplemented:
    forwarding an arbitrary client query string to a URL carrying the shared
    secret was a request-forgery surface with no remaining caller.
    """
    _deprecated(request, "GET /api/catalog")
    if action != "csv":
        raise ValidationError(
            "This endpoint now serves only the local kit catalogue. "
            "Use the documented API routes.",
        )

    catalog_service.ensure_initialised()
    payload = kit_service.catalog_payload()
    if sheet == "kits":
        return payload["kits"]
    if sheet == "troubleshooting":
        return payload["troubleshooting"]
    raise ValidationError(f"Unknown sheet: {sheet}")


@router.post("/api/proxy")
def legacy_proxy_post(
    request: Request,
    payload: dict[str, Any],
    actor: Annotated[Principal, RequireCreateTickets],
) -> dict[str, Any]:
    """Old ticket-save proxy, dispatching on the body's ``action``.

    Three known actions are mapped to the ticket service. Anything else is
    refused: the monolith forwarded unrecognised actions straight to Apps Script,
    which meant the endpoint's real capability set was whatever the script
    happened to implement.
    """
    _deprecated(request, "POST/PATCH/DELETE /api/tickets")
    action = str(payload.get("action") or "")

    if action == "newticket":
        return ticket_service.create(payload, actor=actor.username).to_api()
    if action == "updateticket":
        return ticket_service.update(payload, actor=actor.username).to_api()
    if action == "deleteticket":
        return ticket_service.delete(payload, actor=actor.username).to_api()
    raise ValidationError(f"Unsupported action: {action or '(none)'}")


# -- Analytics -----------------------------------------------------------------
@router.get("/api/analytics/rebuild")
def legacy_analytics_rebuild(
    request: Request,
    actor: Annotated[Principal, RequireSystem],
) -> dict[str, Any]:
    """Old rebuild trigger, which used GET for a mutating action.

    Kept working, but the canonical route is a POST — a GET that rebuilds state
    can be triggered by a prefetch, a link preview or a crawler.
    """
    _deprecated(request, "POST /api/analytics/rebuild")
    return analytics_router.rebuild_view(actor)


# -- Users ---------------------------------------------------------------------
@router.get("/api/users")
def legacy_users_list(
    request: Request,
    actor: Annotated[Principal, RequireManageUsers],
) -> dict[str, Any]:
    _deprecated(request, "GET /api/users")
    return user_service.directory(actor)


@router.post("/api/users/create")
def legacy_users_create(
    request: Request,
    payload: dict[str, Any],
    actor: Annotated[Principal, RequireManageUsers],
) -> dict[str, Any]:
    """Old create, which keyed users by ``username``.

    Accounts are identified by email address, so the old ``username`` field is read
    as the email. A short local-part name from the NAS era will fail validation —
    correctly: that account does not exist in the directory and cannot be created
    without a real address.
    """
    _deprecated(request, "POST /api/users")
    return user_service.create(
        actor,
        email=str(payload.get("username") or payload.get("email") or ""),
        display=str(payload.get("display") or ""),
        password=str(payload.get("password") or ""),
        role=str(payload.get("role") or "tech"),
        permissions=payload.get("permissions") or [],
    )


@router.post("/api/users/update")
def legacy_users_update(
    request: Request,
    payload: dict[str, Any],
    actor: Annotated[Principal, RequireManageUsers],
) -> dict[str, Any]:
    _deprecated(request, "PATCH /api/users/{id}")
    return user_service.update(
        actor,
        user_id=_resolve_user_id(actor, payload),
        display=payload.get("display"),
        role=payload.get("role"),
        permissions=payload.get("permissions"),
        active=payload.get("active"),
    )


@router.post("/api/users/password")
def legacy_users_password(
    request: Request,
    payload: dict[str, Any],
    actor: Annotated[Principal, RequireManageUsers],
) -> dict[str, Any]:
    _deprecated(request, "PUT /api/users/{id}/password")
    return user_service.set_password(
        actor,
        user_id=_resolve_user_id(actor, payload),
        password=str(payload.get("password") or ""),
    )


@router.post("/api/users/delete")
def legacy_users_delete(
    request: Request,
    payload: dict[str, Any],
    actor: Annotated[Principal, RequireManageUsers],
) -> dict[str, Any]:
    _deprecated(request, "DELETE /api/users/{id}")
    return user_service.delete(actor, user_id=_resolve_user_id(actor, payload))


def _resolve_user_id(actor: Principal, payload: dict[str, Any]) -> str:
    """Map a legacy ``username`` to a user id.

    The old API addressed users by name; the user directory addresses them by uuid. The
    directory is small, so a linear scan is fine.
    """
    if payload.get("id"):
        return str(payload["id"])

    wanted = str(payload.get("username") or "").strip().lower()
    if not wanted:
        raise ValidationError("A username or id is required.")

    for user in user_service.directory(actor)["users"]:
        if user["username"].lower() == wanted:
            return user["id"]
    raise NotFound("User not found.")


# -- Roles ---------------------------------------------------------------------
@router.get("/api/roles")
def legacy_roles_list(
    request: Request,
    actor: Annotated[Principal, RequireManageRoles],
) -> dict[str, Any]:
    _deprecated(request, "GET /api/roles")
    return role_service.listing(actor)


@router.post("/api/roles/create")
def legacy_roles_create(
    request: Request,
    payload: roles_router.RoleCreateRequest,
    actor: Annotated[Principal, RequireManageRoles],
) -> dict[str, Any]:
    _deprecated(request, "POST /api/roles")
    return roles_router.create_role(payload, actor)


@router.post("/api/roles/update")
def legacy_roles_update(
    request: Request,
    payload: dict[str, Any],
    actor: Annotated[Principal, RequireManageRoles],
) -> dict[str, Any]:
    _deprecated(request, "PATCH /api/roles/{key}")
    return role_service.update(
        actor,
        key=str(payload.get("key") or ""),
        label=payload.get("label"),
        rank=payload.get("rank"),
        permissions=payload.get("permissions"),
    )


@router.post("/api/roles/delete")
def legacy_roles_delete(
    request: Request,
    payload: dict[str, Any],
    actor: Annotated[Principal, RequireManageRoles],
) -> dict[str, Any]:
    _deprecated(request, "DELETE /api/roles/{key}")
    return role_service.delete(actor, key=str(payload.get("key") or ""))


# -- System --------------------------------------------------------------------
@router.get("/api/status")
def legacy_status(
    request: Request,
    actor: Annotated[Principal, RequireSystem],
) -> dict[str, Any]:
    """Old status endpoint.

    The active-session list it used to return is gone: sessions are signed
    tokens now, and this process does not keep a list of them.
    """
    _deprecated(request, "GET /api/system/info")
    snapshot = telemetry_service.snapshot()
    return {
        "ok": True,
        "uptime": snapshot["host"]["uptime_sec"],
        "storage": snapshot["storage"],
        "active_sessions": [],
        "last_backup": snapshot["store"]["last_nightly"],
    }


@router.get("/api/backup")
def legacy_backup_trigger(request: Request) -> RedirectResponse:
    """Old backup trigger, which used GET for a mutating action.

    A 308 preserves the method, so a client that follows it lands on the POST
    route. Left as a redirect rather than a working GET on purpose: a mutating
    GET must not keep working, because anything that prefetches it fires a backup.
    """
    _deprecated(request, "POST /api/system/backup")
    return RedirectResponse("/api/system/backup", status_code=308)


@router.get("/api/system/backup-info")
def legacy_backup_info(
    request: Request,
    actor: Annotated[Principal, RequireSystem],
    name: str = "",
) -> dict[str, Any]:
    _deprecated(request, "GET /api/system/backups")
    if name:
        return system_router.backup_manifest(name, actor)
    return system_router.list_backups(actor, 15)


@router.get("/api/system/backup-download")
def legacy_backup_download(
    request: Request,
    actor: Annotated[Principal, RequireSystem],
    name: Annotated[str, Query(min_length=1)],
) -> FileResponse:
    _deprecated(request, "GET /api/system/backups/{name}/download")
    return system_router.download_backup(name, actor)


@router.get("/api/system/deadletters")
def legacy_dead_letters(
    request: Request,
    actor: Annotated[Principal, RequireSystem],
    dead: DeadLetters,
) -> dict[str, Any]:
    _deprecated(request, "GET /api/system/deadletters")
    return system_router.list_dead_letters(actor, dead)


@router.post("/api/system/deadletters/replay")
def legacy_dead_letters_replay(
    request: Request,
    payload: system_router.DeadLetterRequest,
    actor: Annotated[Principal, RequireSystem],
    dead: DeadLetters,
    pending: PendingWrites,
) -> dict[str, Any]:
    _deprecated(request, "POST /api/system/deadletters/replay")
    return system_router.replay_dead_letters(payload, actor, dead, pending)


# -- Kits ----------------------------------------------------------------------
@router.get("/api/kit/coverage")
def legacy_coverage(
    request: Request,
    actor: Annotated[Principal, RequireKitEditor],
) -> dict[str, Any]:
    _deprecated(request, "GET /api/kit/coverage")
    return kits_router.coverage(actor)


@router.post("/api/tickets/poll")
def legacy_poll(
    request: Request,
    actor: Annotated[Principal, RequireTickets],
) -> dict[str, Any]:
    _deprecated(request, "POST /api/tickets/poll")
    return tickets_router.force_poll(actor)


@router.get("/api/tickets/poll")
def legacy_poll_get(
    request: Request,
    actor: Annotated[Principal, RequireTickets],
) -> dict[str, Any]:
    """Old poll trigger, which used GET. Kept because the Today pane calls it."""
    _deprecated(request, "POST /api/tickets/poll")
    return tickets_router.force_poll(actor)
