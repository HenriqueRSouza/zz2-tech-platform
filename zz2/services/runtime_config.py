"""Operator-tunable runtime settings.

Two kinds of configuration, kept apart on purpose:

:mod:`zz2.settings`
    Secrets and deployment facts. Environment only, read once, immutable.

This module
    Non-secret operational knobs a super-admin changes from the Settings page
    without a restart: poll intervals, the work window, backup retention.

The monolith mixed both in one ``config.json`` and re-read it on every call —
``_cfg_num`` opened and parsed the file for each of the five interval lookups the
poll loop performed per iteration. Here the document is cached and invalidated on
write.

Each field is declared once as a :class:`ConfigField` carrying its own type,
bounds and label. The monolith expressed the same rules twice: as ``_cfg_num``
clamps when reading, and again as ``setnum`` validation when writing. The two
lists drifted — ``auto_resolve_days`` was writable in the range 1–365 but had no
read-side clamp at all, so a bad value stored by an older build was used
verbatim.
"""

from __future__ import annotations

import datetime as dt
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from zz2.core.errors import StorageError, ValidationError
from zz2.core.jsonstore import read_json, write_json
from zz2.core.logging import audit, logger
from zz2.paths import BACKUPS_DIR, CONFIG_FILE

FieldKind = Literal["int", "float", "bool", "hhmm", "url"]

CONFIG_SNAPSHOT_KEEP = 20


@dataclass(frozen=True, slots=True)
class ConfigField:
    """Declarative definition of one tunable value.

    Attributes:
        key: Key in ``config.json``.
        kind: How to parse, validate and clamp.
        default: Used when absent or unparseable.
        label: Human-readable name, used verbatim in validation messages.
        minimum: Inclusive lower bound (numeric kinds).
        maximum: Inclusive upper bound (numeric kinds).
        validator: Extra check for ``url``; returns an error string or ``None``.
    """

    key: str
    kind: FieldKind
    default: Any
    label: str
    minimum: float | None = None
    maximum: float | None = None
    validator: Callable[[str], str | None] | None = None

    def coerce(self, raw: Any) -> Any:
        """Parse and clamp a stored value, falling back to the default.

        Never raises. This is the *read* path: a value that is out of range on
        disk must not stop the server from starting, it must be pulled back into
        range and used.
        """
        try:
            if self.kind == "bool":
                if isinstance(raw, bool):
                    return raw
                return str(raw).strip().lower() in {"1", "true", "yes", "on"}
            if self.kind in {"int", "float"}:
                value = float(raw)
                if value != value:  # NaN
                    return self.default
                if self.minimum is not None:
                    value = max(self.minimum, value)
                if self.maximum is not None:
                    value = min(self.maximum, value)
                return int(round(value)) if self.kind == "int" else round(value, 2)
            if self.kind == "hhmm":
                return _normalise_hhmm(str(raw)) or self.default
            return str(raw or "").strip()
        except (TypeError, ValueError):
            return self.default

    def validate(self, raw: Any) -> Any:
        """Parse a client-supplied value strictly.

        Raises:
            ValidationError: The value is the wrong type or out of range. This is
                the *write* path: a bad value from the Settings page must be
                rejected with a message, not silently clamped, or the operator
                sees their input accepted and quietly changed.
        """
        if self.kind == "bool":
            return bool(raw)

        if self.kind in {"int", "float"}:
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"{self.label} must be a number.") from exc
            if value != value or (self.minimum is not None and value < self.minimum) \
               or (self.maximum is not None and value > self.maximum):
                raise ValidationError(
                    f"{self.label} must be between {self.minimum} and {self.maximum}."
                )
            return int(round(value)) if self.kind == "int" else round(value, 2)

        if self.kind == "hhmm":
            normalised = _normalise_hhmm(str(raw or ""))
            if normalised is None:
                raise ValidationError(f"{self.label} must be HH:MM (24-hour).")
            return normalised

        text = str(raw or "").strip()
        if text and self.validator is not None:
            problem = self.validator(text)
            if problem:
                raise ValidationError(problem)
        return text


def _normalise_hhmm(raw: str) -> str | None:
    """``"9:5"`` becomes ``"09:05"``; anything invalid returns ``None``."""
    try:
        hours_text, minutes_text = raw.strip().split(":")
        hours, minutes = int(hours_text), int(minutes_text)
    except ValueError:
        return None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        return None
    return f"{hours:02d}:{minutes:02d}"


def _validate_appscript_url(url: str) -> str | None:
    """Restrict the Apps Script URL to real Apps Script deployments.

    This URL is fetched *server-side* with the shared secret attached, so an
    unconstrained value is a server-side request forgery primitive: a super-admin
    — or anyone who reaches that endpoint — could point it at an internal address
    and have the server fetch it, secret included, and return the body.
    """
    if not url.startswith("https://script.google.com/"):
        return "Apps Script URL must start with https://script.google.com/"
    if "/exec" not in url:
        return "Apps Script URL must be an /exec deployment link"
    return None


#: Every tunable value. Adding one here makes it readable, validated and
#: writable through the Settings page with no other change.
CONFIG_FIELDS: tuple[ConfigField, ...] = (
    ConfigField("poll_interval", "int", 30, "Sheet poll interval", 5, 3600),
    ConfigField("pane_refresh_interval", "int", 30, "Today pane refresh", 2, 3600),
    ConfigField("reconcile_interval", "int", 300, "Reconcile interval", 30, 86400),
    ConfigField("reconcile_days", "int", 2, "Reconcile window", 1, 30),
    ConfigField("auto_resolve_days", "int", 7, "Auto-resolve threshold", 1, 365),
    ConfigField("auto_resolve_enabled", "bool", True, "Auto-resolve enabled"),
    ConfigField("backup_keep", "int", 30, "Backups to keep", 3, 365),
    ConfigField("backup_include_media", "bool", False, "Include media in backups"),
    ConfigField("work_window_enabled", "bool", False, "Work window enabled"),
    ConfigField("work_start", "hhmm", "09:30", "Work start"),
    ConfigField("work_end", "hhmm", "20:00", "Work end"),
    ConfigField("appscript_url", "url", "", "Apps Script URL",
                validator=_validate_appscript_url),
)

_FIELDS_BY_KEY = {field.key: field for field in CONFIG_FIELDS}

#: Keys that must never be accepted from an HTTP request, whatever the caller's
#: permissions. Secrets belong in the environment; a browser-writable secret is a
#: browser-readable secret one bug later.
FORBIDDEN_KEYS = frozenset({
    "secret_key", "database_url", "session_secret", "saml_session_secret",
    "azure_storage_connection_string", "microsoft_client_secret",
    # Pre-migration keys, still refused so an old config.json cannot smuggle them in.
    "supabase_service_key", "supabase_jwt_secret",
    "supabase_publishable_key", "supabase_url",
})


class RuntimeConfigService:
    """Cached, validated access to ``config.json``."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: dict[str, Any] | None = None
        self._cache_mtime: float | None = None

    # -- Reading ---------------------------------------------------------------
    def _document(self) -> dict[str, Any]:
        """The raw document, reloaded only when the file changed on disk.

        The mtime check keeps an out-of-band edit (an operator with an editor, a
        restore from backup) working without a restart, while normal reads stay
        in memory.
        """
        with self._lock:
            try:
                mtime = CONFIG_FILE.stat().st_mtime
            except OSError:
                mtime = None
            if self._cache is None or mtime != self._cache_mtime:
                document = read_json(CONFIG_FILE, {})
                self._cache = document if isinstance(document, dict) else {}
                self._cache_mtime = mtime
            return self._cache

    def get(self, key: str) -> Any:
        """One tunable value, coerced and clamped.

        Raises:
            KeyError: ``key`` is not a declared field. A typo becomes an error
                here rather than a silent default.
        """
        field = _FIELDS_BY_KEY.get(key)
        if field is None:
            raise KeyError(f"Unknown configuration key: {key}")
        document = self._document()
        if key not in document:
            return field.default
        return field.coerce(document[key])

    def as_dict(self) -> dict[str, Any]:
        """Every tunable value, for the Settings page."""
        return {field.key: self.get(field.key) for field in CONFIG_FIELDS}

    # -- Convenience accessors used on hot paths -------------------------------
    @property
    def poll_interval(self) -> int:
        return int(self.get("poll_interval"))

    @property
    def reconcile_interval(self) -> int:
        return int(self.get("reconcile_interval"))

    @property
    def reconcile_days(self) -> int:
        return int(self.get("reconcile_days"))

    @property
    def pane_refresh_interval(self) -> int:
        return int(self.get("pane_refresh_interval"))

    @property
    def auto_resolve_days(self) -> int:
        return int(self.get("auto_resolve_days"))

    @property
    def auto_resolve_enabled(self) -> bool:
        return bool(self.get("auto_resolve_enabled"))

    @property
    def backup_keep(self) -> int:
        return int(self.get("backup_keep"))

    @property
    def backup_include_media(self) -> bool:
        return bool(self.get("backup_include_media"))

    @property
    def appscript_url(self) -> str:
        return str(self.get("appscript_url"))

    def within_work_window(self, now: dt.datetime | None = None) -> bool:
        """Whether the poller may run right now.

        With the window disabled — the default — always true. Enabled, the poller
        pauses outside the configured hours to spare Apps Script quota. Windows
        that wrap past midnight are handled explicitly.
        """
        if not self.get("work_window_enabled"):
            return True
        moment = now or dt.datetime.now()
        current = moment.hour * 3600 + moment.minute * 60 + moment.second
        start = _hhmm_to_seconds(str(self.get("work_start")))
        end = _hhmm_to_seconds(str(self.get("work_end")))
        if start <= end:
            return start <= current < end
        return current >= start or current < end

    # -- Writing ---------------------------------------------------------------
    def apply(self, changes: dict[str, Any], *, actor: str) -> list[str]:
        """Validate and persist a partial update.

        Unknown and forbidden keys are rejected rather than ignored, so a caller
        that misspells a key or tries to write a secret finds out. The whole
        payload is validated before anything is written: a request that sets one
        good and one bad value changes nothing.

        Returns:
            The keys whose value actually changed.

        Raises:
            ValidationError: A key is unknown, forbidden, or its value invalid.
            StorageError: The write failed.
        """
        if not isinstance(changes, dict):
            raise ValidationError("Expected an object of configuration values.")

        forbidden = FORBIDDEN_KEYS & changes.keys()
        if forbidden:
            raise ValidationError(
                "These values are set in the server environment and cannot be "
                f"changed here: {', '.join(sorted(forbidden))}."
            )
        unknown = changes.keys() - _FIELDS_BY_KEY.keys()
        if unknown:
            raise ValidationError(f"Unknown settings: {', '.join(sorted(unknown))}.")

        validated = {key: _FIELDS_BY_KEY[key].validate(value)
                     for key, value in changes.items()}

        with self._lock:
            document = dict(self._document())
            changed = [key for key, value in validated.items() if document.get(key) != value]
            if not changed:
                return []

            self._snapshot()
            document.update(validated)
            write_json(CONFIG_FILE, document)
            self._cache = document
            try:
                self._cache_mtime = CONFIG_FILE.stat().st_mtime
            except OSError as exc:
                raise StorageError(log_detail=f"config stat after write: {exc}") from exc

        audit("config.changed", actor=actor, keys=changed)
        logger.info("runtime configuration updated", extra={"keys": changed})
        return changed

    def _snapshot(self) -> None:
        """Copy the current config aside before overwriting it.

        Best effort: a failed snapshot must not block the change. Kept in its own
        retention bucket so these small files can never crowd out a full backup.
        """
        if not CONFIG_FILE.exists():
            return
        try:
            directory = BACKUPS_DIR / "config"
            directory.mkdir(parents=True, exist_ok=True, mode=0o750)
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(CONFIG_FILE, directory / f"config_{stamp}.json")
            snapshots = sorted(directory.glob("config_*.json"))
            for stale in snapshots[:-CONFIG_SNAPSHOT_KEEP]:
                stale.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("config snapshot failed", extra={"reason": str(exc)})


def _hhmm_to_seconds(value: str) -> int:
    normalised = _normalise_hhmm(value) or "00:00"
    hours, minutes = normalised.split(":")
    return int(hours) * 3600 + int(minutes) * 60


runtime_config = RuntimeConfigService()
"""Process-wide instance. Injected in tests by constructing a fresh service."""
