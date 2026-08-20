"""FastAPI dependencies — the authorization boundary.

Every handler in the monolith opened with the same four lines::

    token = self._get_token()
    session = _get_session(token) if token else None
    if not _has_perm(_session_user(session), 'kit_editor'):
        return self._json(403, {'error': 'Forbidden'})

Repeated across roughly seventy methods, which is why the omissions were hard to
see. ``_handle_config`` had no check at all. ``_handle_proxy`` allowed
``action=csv`` unauthenticated. ``_handle_draft_get`` checked only that a session
existed and never which permission it carried.

Declaring the requirement in the signature makes it impossible to forget: a route
without a principal dependency takes no principal argument, so an unprotected
route is visible in its own definition rather than absent from its body.

    @router.post("/api/kit/save")
    def save_kit(actor: Annotated[Principal, RequireKitEditor]) -> dict:
        ...

The dependency raises; it never returns ``None``. A handler that received a
principal is a handler whose caller was authorized.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from zz2.core.errors import AuthenticationError, PermissionDenied
from zz2.core.logging import actor_var
from zz2.domain.permissions import Permission, Principal
from zz2.repositories.drafts import DraftRepository
from zz2.repositories.kit_history import KitHistoryRepository
from zz2.repositories.kits import KitCatalogRepository
from zz2.repositories.roles import RoleRepository
from zz2.repositories.stats import StatsRepository
from zz2.repositories.tickets import TicketStoreRepository
from zz2.repositories.write_queue import DeadLetterRepository, PendingWriteRepository
from zz2.services.auth import auth_service, record_denial


# -- Authentication ------------------------------------------------------------
def current_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Resolve the caller, or reject the request.

    Raises:
        AuthenticationError: No usable bearer token, or verification failed.
    """
    token = auth_service.token_from_header(authorization)
    if not token:
        raise AuthenticationError()

    principal = auth_service.authenticate(token)

    # Bind the actor to the logging context so every record produced while
    # handling this request names who caused it, without threading it through.
    actor_var.set(principal.username)
    request.state.principal = principal
    return principal


CurrentPrincipal = Depends(current_principal)


# -- Authorization -------------------------------------------------------------
class RequirePermission:
    """Dependency factory asserting one permission.

    A class rather than a closure so FastAPI can cache the instance and so the
    requirement is introspectable — the permission is an attribute, which lets a
    test enumerate what every route requires.
    """

    def __init__(self, permission: Permission) -> None:
        self.permission = permission

    def __call__(self, principal: Annotated[Principal, CurrentPrincipal],
                 request: Request) -> Principal:
        if not principal.has(self.permission):
            record_denial(principal, self.permission.value, request.url.path)
            raise PermissionDenied()
        return principal


RequireTickets = Depends(RequirePermission(Permission.TICKETS))
RequireCreateTickets = Depends(RequirePermission(Permission.CREATE_TICKETS))
RequireKitEditor = Depends(RequirePermission(Permission.KIT_EDITOR))
RequireAnalytics = Depends(RequirePermission(Permission.ANALYTICS))
RequireDashboard = Depends(RequirePermission(Permission.DASHBOARD))
RequireManageUsers = Depends(RequirePermission(Permission.MANAGE_USERS))
RequireManageRoles = Depends(RequirePermission(Permission.MANAGE_ROLES))
RequireSystem = Depends(RequirePermission(Permission.SYSTEM_SETTINGS))


def require_root(principal: Annotated[Principal, RequireSystem]) -> Principal:
    """Assert a root role, for irreversible operations.

    ``system_settings`` is not sufficient for restoring a backup or rewriting
    configuration: those can destroy state or lock everyone out, and a custom role
    could be granted the permission without anyone intending that reach.
    """
    if not principal.is_root:
        raise PermissionDenied("This action requires a super-admin account.")
    return principal


RequireRoot = Depends(require_root)


# -- Repositories --------------------------------------------------------------
# Constructed per request. They hold no connection state — just a path and a lock
# looked up from a registry — so this is cheap, and it keeps tests able to
# override any single dependency without touching module globals.
def get_ticket_store() -> TicketStoreRepository:
    return TicketStoreRepository()


def get_catalog() -> KitCatalogRepository:
    return KitCatalogRepository()


def get_roles() -> RoleRepository:
    return RoleRepository()


def get_stats() -> StatsRepository:
    return StatsRepository()


def get_drafts() -> DraftRepository:
    return DraftRepository()


def get_kit_history() -> KitHistoryRepository:
    return KitHistoryRepository()


def get_pending_writes() -> PendingWriteRepository:
    return PendingWriteRepository()


def get_dead_letters() -> DeadLetterRepository:
    return DeadLetterRepository()


TicketStore = Annotated[TicketStoreRepository, Depends(get_ticket_store)]
Roles = Annotated[RoleRepository, Depends(get_roles)]
Stats = Annotated[StatsRepository, Depends(get_stats)]
Drafts = Annotated[DraftRepository, Depends(get_drafts)]
PendingWrites = Annotated[PendingWriteRepository, Depends(get_pending_writes)]
DeadLetters = Annotated[DeadLetterRepository, Depends(get_dead_letters)]
