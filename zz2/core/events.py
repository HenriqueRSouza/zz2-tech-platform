"""In-process event bus.

Solves a concrete layering problem. In the monolith the *persistence* primitive
knew about the *sync service*::

    if path in (KITS_JSON, TS_JSON, BLUEPRINTS_JSON) \\
       and '_schedule_auxiliary_sync' in globals():
        _schedule_auxiliary_sync()

``_save_json`` — the lowest-level function in the file — reached up two layers to
trigger a Supabase upload, and needed a ``globals()`` probe to avoid exploding
during startup when the function it wanted did not exist yet. Any new caller of
``_save_json`` inherited that behaviour whether it wanted it or not.

Inverted here: repositories publish a fact ("the kit catalogue changed") and know
nothing about who listens. The mirror service subscribes at startup. Removing the
mirror is deleting one subscription, not auditing every write path.

Handlers run synchronously on the publishing thread and their exceptions are
logged, never propagated — a failing mirror upload must not roll back the local
save that triggered it.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from zz2.core.logging import logger


@dataclass(frozen=True, slots=True)
class Event:
    """Base class for published facts. Immutable, so handlers cannot mutate it."""


@dataclass(frozen=True, slots=True)
class KitCatalogChanged(Event):
    """Kits, troubleshooting trees or blueprints were written locally."""

    reason: str


@dataclass(frozen=True, slots=True)
class TicketStoreChanged(Event):
    """The local ticket store gained, lost or altered rows."""

    reason: str
    ticket_number: str | None = None


@dataclass(frozen=True, slots=True)
class MediaChanged(Event):
    """A manual or diagram image was added or replaced."""

    kit_key: str
    kind: str


Handler = Callable[[Event], None]


class EventBus:
    """Synchronous publish/subscribe keyed on event type."""

    def __init__(self) -> None:
        self._handlers: dict[type[Event], list[Handler]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, event_type: type[Event], handler: Handler) -> None:
        with self._lock:
            self._handlers[event_type].append(handler)

    def publish(self, event: Event) -> None:
        """Deliver ``event`` to its subscribers.

        A handler that raises is logged and skipped. The publisher has already
        committed its own work; a subscriber failure is an independent problem.
        """
        with self._lock:
            handlers = list(self._handlers.get(type(event), ()))
        for handler in handlers:
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - one bad subscriber must not break others
                logger.exception(
                    "event handler failed",
                    extra={"event": type(event).__name__,
                           "handler": getattr(handler, "__qualname__", repr(handler))},
                )

    def reset(self) -> None:
        """Drop every subscription. Test-support only."""
        with self._lock:
            self._handlers.clear()


bus = EventBus()
"""Process-wide bus. Injected explicitly in tests via :meth:`EventBus.reset`."""
