"""System administration endpoints.

Split by blast radius. Most routes need ``system_settings``; the ones that can
destroy state or lock everyone out — restore, configuration writes — additionally
require a root role, because ``system_settings`` can be granted to a custom role
without anyone intending that reach.

Three endpoints from the monolith are **gone**, and their absence is deliberate:

``POST /api/system/restart``
    Re-executed the process from a request. On a VPS restarts belong to systemd
    (``systemctl restart zz2``), which handles ordering, socket handover and
    failure policy. A web-triggered ``os.execv`` also meant one bug in the auth
    check was a remote code-execution primitive.

``POST /api/system/revoke`` and ``/api/system/clear-expired``
    Managed the local ``sessions.json``, which no longer exists. Sessions are
    signed tokens issued by this server; revocation happens at sign-out.

``POST /api/system/unlock``
    Cleared the local login-lockout table. The brute-force guard for passwords,
    bearer tokens and SSO responses is managed under
    ``/api/system/security/lockouts`` instead.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from zz2.api.deps import (
    DeadLetters,
    PendingWrites,
    RequireDashboard,
    RequireRoot,
    RequireSystem,
    TicketStore,
)
from zz2.core.errors import NotFound
from zz2.core.logging import audit
from zz2.domain.permissions import Principal
from zz2.paths import AUDIT_LOG, SERVER_LOG
from zz2.services.analytics import analytics_service
from zz2.services.autoresolve import auto_resolve_service
from zz2.services.backup import backup_service
from zz2.services.lockout import lockout_guard
from zz2.services.mirror import mirror_service
from zz2.services.runtime_config import runtime_config
from zz2.services.sheet_sync import sheet_sync_service
from zz2.services.telemetry import telemetry_service
from zz2.workers.manager import worker_manager

router = APIRouter(prefix="/api/system", tags=["system"])

AUTORESOLVE_REQUEST_BUDGET_SECONDS = 8.0
"""Wall-clock budget for an operator-triggered auto-resolve.

Short, and the response reports ``remaining`` so the page can call again. The
nightly job passes no budget and runs to completion.
"""


class ConfigUpdateRequest(BaseModel):
    """A partial runtime-configuration write.

    Keys are validated against the declared field set in
    :mod:`zz2.services.runtime_config`; unknown and secret keys are rejected
    rather than ignored.
    """

    model_config = ConfigDict(extra="allow")

    def changes(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class RestoreRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=64)
    files: list[str] | str = "all"


class DeadLetterRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: str = ""
    """Blank replays everything."""


class AutoResolveRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    days: int | None = Field(default=None, ge=1, le=365)
    apply: bool = False


# -- Telemetry -----------------------------------------------------------------
@router.get("/info")
def system_info(actor: Annotated[Principal, RequireSystem]) -> dict[str, Any]:
    """Host, process, store and configuration state."""
    return telemetry_service.snapshot()


@router.get("/sync-health")
def sync_health(
    actor: Annotated[Principal, RequireDashboard],
    store: TicketStore,
    pending: PendingWrites,
    dead: DeadLetters,
) -> dict[str, Any]:
    """Write-pipeline health.

    ``dashboard`` rather than ``system_settings``: a technician whose ticket is
    stuck in the queue benefits from seeing that, and it exposes counts only.
    """
    return {
        "ok": True,
        "pending_writes": pending.count(),
        "dead_letters": dead.count(),
        "oldest_pending_age_sec": pending.oldest_age_seconds(),
        "last_poll": telemetry_service.last_poll_at or None,
        "last_reconcile": telemetry_service.last_reconcile_at or None,
        "poll_added_total": telemetry_service.poll_added_total,
        "ticket_count": len(store.rows()),
        "store_generated": store.generated_at(),
        "workers": worker_manager.status(),
    }


@router.get("/logs")
def recent_logs(
    actor: Annotated[Principal, RequireSystem],
    channel: Annotated[str, Query(pattern="^(server|audit)$")] = "server",
    lines: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Tail one log channel.

    The channel is a constrained pattern, not a path. Accepting a filename here
    would be an arbitrary-file-read endpoint with a plausible-looking parameter.
    """
    path = SERVER_LOG if channel == "server" else AUDIT_LOG
    return {"ok": True, "channel": channel,
            "lines": telemetry_service.tail_log(path, lines)}


# -- Configuration -------------------------------------------------------------
@router.get("/config")
def read_config(actor: Annotated[Principal, RequireSystem]) -> dict[str, Any]:
    """Current runtime configuration.

    Tunables only. Secrets live in the environment and are never readable through
    the API — the System page shows *whether* each is configured, via
    ``/api/system/info``, not its value.
    """
    return {"ok": True, "config": runtime_config.as_dict()}


@router.post("/config")
def write_config(
    payload: ConfigUpdateRequest,
    actor: Annotated[Principal, RequireRoot],
) -> dict[str, Any]:
    """Update runtime configuration. Root only.

    The previous file is snapshotted before the write, so a bad edit is
    recoverable from ``backups/config/``.
    """
    changed = runtime_config.apply(payload.changes(), actor=actor.username)
    return {"ok": True, "changed": changed}


# -- Backups -------------------------------------------------------------------
@router.post("/backup", status_code=202)
def start_backup(
    actor: Annotated[Principal, RequireSystem],
    background: BackgroundTasks,
) -> dict[str, Any]:
    """Start a backup and return immediately.

    ``202`` with the work in the background: a media-inclusive backup copies
    hundreds of megabytes and would otherwise hold a request open past any
    reasonable proxy timeout.
    """
    background.add_task(backup_service.run, label=f"manual:{actor.username}")
    return {"ok": True, "message": "Backup started."}


@router.get("/backups")
def list_backups(
    actor: Annotated[Principal, RequireSystem],
    limit: Annotated[int, Query(ge=1, le=100)] = 15,
) -> dict[str, Any]:
    return {"ok": True, "backups": [item.to_api() for item in backup_service.list(limit)]}


@router.get("/backups/{name}")
def backup_manifest(
    name: str,
    actor: Annotated[Principal, RequireSystem],
) -> dict[str, Any]:
    """One snapshot's manifest, listing every file with its hash."""
    manifest = backup_service.manifest(name)
    if manifest is None:
        raise NotFound("Backup not found.")
    return {"ok": True, "manifest": manifest}


@router.get("/backups/{name}/download")
def download_backup(
    name: str,
    actor: Annotated[Principal, RequireSystem],
) -> FileResponse:
    """Download a snapshot as a zip."""
    archive = backup_service.bundle(name)
    audit("backup.downloaded", actor=actor.username, name=name)
    return FileResponse(archive, media_type="application/zip", filename=archive.name)


@router.post("/restore")
def restore_backup(
    payload: RestoreRequest,
    actor: Annotated[Principal, RequireRoot],
) -> dict[str, Any]:
    """Restore files from a snapshot. Root only.

    The most destructive operation the application offers — it overwrites live
    state. A ``pre-restore`` snapshot is taken first, so the restore itself is
    reversible.
    """
    return backup_service.restore(payload.name, payload.files, actor=actor.username)


# -- Store maintenance ---------------------------------------------------------
@router.post("/rebuild")
def rebuild_store(actor: Annotated[Principal, RequireSystem]) -> dict[str, Any]:
    """Re-export every ticket from the sheet and rebuild the analytics view.

    Expensive — a full export plus a full consolidation. Restricted to
    ``system_settings`` for that reason, not because the data is more sensitive
    than what ``/api/analytics`` already serves.
    """
    count = analytics_service.rebuild_from_sheet()
    audit("store.rebuilt", actor=actor.username, tickets=count)
    return {"ok": True, "tickets": count}


@router.post("/reconcile")
def reconcile_store(
    actor: Annotated[Principal, RequireSystem],
    days: Annotated[int | None, Query(ge=1, le=30)] = None,
) -> dict[str, Any]:
    """Reconcile the recent window against the sheet now."""
    removed = sheet_sync_service.reconcile(days)
    telemetry_service.record_reconcile()
    return {"ok": True, "removed": removed}


@router.post("/autoresolve")
def run_autoresolve(
    payload: AutoResolveRequest,
    actor: Annotated[Principal, RequireSystem],
) -> dict[str, Any]:
    """Preview or apply auto-resolution of stale tickets."""
    if not payload.apply:
        return auto_resolve_service.preview(payload.days)
    result = auto_resolve_service.apply(
        payload.days,
        time_budget=AUTORESOLVE_REQUEST_BUDGET_SECONDS,
        actor=actor.username,
    )
    return result.to_api()


@router.post("/sync-database")
@router.post("/sync-supabase", include_in_schema=False)  # pre-migration path
def force_mirror(actor: Annotated[Principal, RequireSystem]) -> dict[str, Any]:
    """Push the kit catalogue to the database now."""
    counts = mirror_service.upload(reason=f"manual:{actor.username}")
    return {"ok": True, "synced": counts}


# -- Dead letters --------------------------------------------------------------
@router.get("/deadletters")
def list_dead_letters(
    actor: Annotated[Principal, RequireSystem],
    dead: DeadLetters,
) -> dict[str, Any]:
    """Writes that exhausted their retries.

    Capped at the most recent 200. The full file holds up to 500, but the older
    entries are the least actionable and the page cannot usefully render them all.
    """
    entries = dead.all()
    items = [
        {
            "id": entry.get("id"),
            "action": entry.get("action"),
            "ticketNum": entry.get("predicted_num")
                         or (entry.get("params") or {}).get("ticketNum"),
            "client": (entry.get("params") or {}).get("client"),
            "product": (entry.get("params") or {}).get("product"),
            "reason": entry.get("dead_reason"),
            "attempts": entry.get("attempts"),
            "created": entry.get("created_at"),
            "dead_at": entry.get("dead_at"),
        }
        for entry in entries[-200:]
    ]
    return {"ok": True, "count": len(entries), "items": items}


@router.post("/deadletters/replay")
def replay_dead_letters(
    payload: DeadLetterRequest,
    actor: Annotated[Principal, RequireSystem],
    dead: DeadLetters,
    pending: PendingWrites,
) -> dict[str, Any]:
    """Move dead-lettered writes back onto the queue.

    Attempt counters are reset, so a replayed operation gets a full set of retries
    rather than dead-lettering again on its first failure.
    """
    entries = dead.drain()
    wanted = payload.id.strip()

    replayed = 0
    kept = 0
    for operation in entries:
        if not wanted or operation.id == wanted:
            pending.enqueue(operation.model_copy(update={"attempts": 0}))
            replayed += 1
        else:
            dead.add(operation, "not selected for replay")
            kept += 1

    audit("write.dead_letters_replayed", actor=actor.username, replayed=replayed)
    return {"ok": True, "replayed": replayed, "remaining": kept}


@router.post("/deadletters/clear")
def clear_dead_letters(
    actor: Annotated[Principal, RequireSystem],
    dead: DeadLetters,
) -> dict[str, Any]:
    """Discard dead-lettered writes permanently."""
    cleared = dead.clear()
    audit("write.dead_letters_cleared", actor=actor.username, cleared=cleared)
    return {"ok": True, "cleared": cleared}


# -- Brute-force guard ---------------------------------------------------------
class LockoutReleaseRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    address: str = Field(min_length=1, max_length=64)


@router.get("/security/lockouts")
def list_lockouts(actor: Annotated[Principal, RequireSystem]) -> dict[str, Any]:
    """Addresses currently blocked for presenting too many bad credentials."""
    return {"ok": True, "lockouts": lockout_guard.blocked()}


@router.post("/security/lockouts/release")
def release_lockout(
    payload: LockoutReleaseRequest,
    actor: Annotated[Principal, RequireSystem],
) -> dict[str, Any]:
    """Unblock one address early, e.g. an office NAT a broken script tripped."""
    released = lockout_guard.release(payload.address)
    if not released:
        raise NotFound("That address is not blocked.")
    audit("auth.lockout_released", actor=actor.username, client=payload.address)
    return {"ok": True}
