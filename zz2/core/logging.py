"""Structured logging and the audit trail.

Two channels, because they answer different questions and have different
retention needs:

``zz2`` (server log)
    Operational. What the process is doing, what failed, how long it took.

``zz2.audit`` (audit log)
    Security-relevant facts: sign-ins, permission denials, privileged actions,
    configuration changes. Append-only, written to its own file so it can be
    shipped or retained independently of the noisy operational log.

Every record carries a ``request_id`` pulled from a :class:`~contextvars.ContextVar`,
so a client that reports "it failed at 14:32" can be traced through the log
without correlating by timestamp and hoping.

Redaction happens in a filter rather than at each call site. The monolith had a
``_redact`` helper that callers had to remember to use; the one place it was
forgotten (``PROXY_GET`` on the failure path) is exactly where the URL carrying
the secret key got logged.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import re
import time
from typing import Any

from zz2.paths import AUDIT_LOG, SERVER_LOG, ensure_runtime_dirs

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
actor_var: contextvars.ContextVar[str] = contextvars.ContextVar("actor", default="-")

logger = logging.getLogger("zz2")
audit_logger = logging.getLogger("zz2.audit")

#: Patterns whose *value* must never be written to disk. Matched against the
#: fully-rendered message, so a secret that arrives inside a URL, a JSON blob or
#: a repr is caught the same way.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(key|apikey|api_key|secret|secret_key|token|password|authorization)"
               r"\s*[=:]\s*[\"']?([^\s\"'&,}]{4,})"),
    re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._\-]{8,})"),
    re.compile(r"\beyJ[A-Za-z0-9._\-]{16,}"),  # bare JWT
    re.compile(r"\bsb_(?:secret|publishable)_[A-Za-z0-9._\-]{8,}"),
)

_REDACTED = "[REDACTED]"


def scrub(text: str) -> str:
    """Replace anything that looks like a credential with a placeholder."""
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: f"{m.group(1)}={_REDACTED}", text)
        else:
            text = pattern.sub(_REDACTED, text)
    return text


class RedactionFilter(logging.Filter):
    """Scrub credentials from the rendered message and from ``extra`` values."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = scrub(str(record.msg))
            if record.args:
                record.msg = scrub(record.getMessage())
                record.args = ()
        except Exception:  # noqa: BLE001 - logging must never raise
            record.msg = "[log redaction failed]"
            record.args = ()
        return True


class ContextFilter(logging.Filter):
    """Attach the ambient request id and actor to every record.

    ``actor`` is filled in only when the caller did not supply one. This is not a
    detail: :func:`audit` passes the acting username explicitly, and an earlier
    version of this filter overwrote it with the ambient value — which silently
    stripped the actor from every audit record.

    The ambient value is also unreliable on its own. FastAPI runs ``def``
    endpoints and their dependencies in a worker thread with a *copied* context,
    so a ``ContextVar.set`` inside a sync dependency is invisible to the caller.
    Explicit beats ambient here, and the request-scoped principal on
    ``request.state`` is what the access log uses.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = request_id_var.get()
        if not getattr(record, "actor", None):
            record.actor = actor_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line — parseable by journald, Loki, or ``jq``."""

    _RESERVED = frozenset(
        vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
        | {"request_id", "actor", "message", "asctime", "taskName"}
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "actor": getattr(record, "actor", "-"),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Readable single line, for local development.

    Structured fields are appended as ``key=value`` rather than dropped. A
    ``authz.denied`` line that does not say *which* permission, or a ``request``
    line without the status, is barely worth writing — and in text mode the
    ``extra`` dict is invisible unless the formatter renders it.
    """

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s [%(request_id)s] %(actor)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = " ".join(
            f"{key}={value}"
            for key, value in sorted(record.__dict__.items())
            if key not in JsonFormatter._RESERVED and not key.startswith("_")
        )
        return f"{line} {extras}" if extras else line


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Wire up both channels. Idempotent, so a reload cannot double-log."""
    ensure_runtime_dirs()
    formatter: logging.Formatter = JsonFormatter() if json_output else TextFormatter()

    for target, path, propagate in ((logger, SERVER_LOG, False), (audit_logger, AUDIT_LOG, False)):
        target.setLevel(level)
        target.propagate = propagate
        for existing in list(target.handlers):
            target.removeHandler(existing)
            existing.close()

        # Rotation matters more than it looks: an unbounded log on a small VPS
        # disk eventually fills the partition, and a full disk takes the whole
        # service down (atomic writes need room for the temp file).
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8", delay=True
        )
        file_handler.setFormatter(formatter)
        target.addHandler(file_handler)

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        target.addHandler(stream_handler)

        target.addFilter(ContextFilter())
        target.addFilter(RedactionFilter())

    # uvicorn's own access log is redundant: the RequestLogMiddleware records the
    # same requests with a request id and an actor attached.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("uvicorn.error").handlers = logger.handlers


def audit(event: str, **fields: Any) -> None:
    """Record one security-relevant fact.

    Args:
        event: Stable event name, e.g. ``"ticket.created"``, ``"config.changed"``.
        **fields: Structured context. Pass ``actor`` for anything a person did —
            an audit record without one answers "what happened" but not "who",
            which is the question an audit log exists for. Credentials are
            scrubbed by the filter, but do not pass them in the first place.
    """
    audit_logger.info(event, extra={"event": event, **fields})
