"""Who is looking at a ticket, and ephemeral events between them.

Replaces Supabase Realtime presence and broadcast. In memory, in this process:
the deployment runs a single worker, and presence is the textbook example of
state that is worthless after a restart — every viewer re-announces itself within
one polling interval.

* A **viewer** is one browser tab (``client_id``) on one ticket. It stays listed
  while it keeps sending heartbeats and expires :data:`VIEWER_TTL_SECONDS` after
  the last one.
* An **event** is a small JSON message for the other viewers of the same ticket
  ("I am editing the notes", "I saved, reload"). Events are kept for
  :data:`EVENT_TTL_SECONDS` and delivered once to each other viewer on its next
  heartbeat — never back to the tab that sent it.

Viewers are keyed on ``(username, client_id)``, not the bare client id the browser
chose, so one signed-in user cannot evict or impersonate another user's tab by
sending its id.
"""

from __future__ import annotations

import datetime as dt
import itertools
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

VIEWER_TTL_SECONDS = 30.0
EVENT_TTL_SECONDS = 60.0

MAX_TICKETS = 5_000
"""Distinct tickets tracked at once. Beyond it the least recently touched ticket is
dropped — a bound on memory, not a limit anyone reaches legitimately."""

MAX_VIEWERS_PER_TICKET = 100
MAX_EVENTS_PER_TICKET = 200


def _iso(timestamp: float) -> str:
    moment = dt.datetime.fromtimestamp(timestamp, dt.UTC).replace(tzinfo=None)
    return moment.isoformat(timespec="seconds") + "Z"


@dataclass(slots=True)
class _Viewer:
    client_id: str
    name: str
    since: float
    last_seen: float
    cursor: int = 0
    """Highest event id already delivered to this viewer."""


@dataclass(slots=True)
class _Event:
    id: int
    owner: tuple[str, str]
    name: str
    type: str
    payload: Any
    at: float


@dataclass(slots=True)
class _Room:
    viewers: dict[tuple[str, str], _Viewer] = field(default_factory=dict)
    events: list[_Event] = field(default_factory=list)
    touched: float = 0.0


class PresenceHub:
    """Thread-safe, in-memory presence for tickets."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._rooms: dict[str, _Room] = {}
        self._ids = itertools.count(1)

    # -- Public API ------------------------------------------------------------
    def heartbeat(self, ticket: str, *, user: str, client_id: str,
                  name: str) -> dict[str, Any]:
        """Register or refresh a viewer; return the viewers and undelivered events."""
        now = self._clock()
        key = (user, client_id)
        with self._lock:
            room = self._room(ticket, now)
            viewer = room.viewers.get(key)
            if viewer is None:
                if len(room.viewers) >= MAX_VIEWERS_PER_TICKET:
                    oldest = min(room.viewers, key=lambda k: room.viewers[k].last_seen)
                    del room.viewers[oldest]
                viewer = _Viewer(client_id=client_id, name=name, since=now, last_seen=now)
                room.viewers[key] = viewer
            else:
                viewer.name = name
                viewer.last_seen = now

            pending = [event for event in room.events
                       if event.id > viewer.cursor and event.owner != key]
            if room.events:
                viewer.cursor = room.events[-1].id

            viewers = sorted(room.viewers.values(), key=lambda v: (v.since, v.client_id))
            return {
                "viewers": [{"client_id": v.client_id, "name": v.name, "since": _iso(v.since)}
                            for v in viewers],
                "events": [{"id": e.id, "client_id": e.owner[1], "name": e.name,
                            "type": e.type, "payload": e.payload, "at": _iso(e.at)}
                           for e in pending],
            }

    def leave(self, ticket: str, *, user: str, client_id: str) -> None:
        with self._lock:
            room = self._rooms.get(ticket)
            if room is None:
                return
            room.viewers.pop((user, client_id), None)
            if not room.viewers and not room.events:
                del self._rooms[ticket]

    def publish(self, ticket: str, *, user: str, client_id: str, event_type: str,
                payload: Any, name: str = "") -> None:
        now = self._clock()
        key = (user, client_id)
        with self._lock:
            room = self._room(ticket, now)
            viewer = room.viewers.get(key)
            room.events.append(_Event(
                id=next(self._ids), owner=key,
                name=viewer.name if viewer else name,
                type=event_type, payload=payload, at=now,
            ))
            if len(room.events) > MAX_EVENTS_PER_TICKET:
                del room.events[:len(room.events) - MAX_EVENTS_PER_TICKET]

    def reset(self) -> None:
        """Forget everything. For tests."""
        with self._lock:
            self._rooms.clear()

    # -- Internals (call with the lock held) -------------------------------------
    def _room(self, ticket: str, now: float) -> _Room:
        self._sweep(now)
        room = self._rooms.get(ticket)
        if room is None:
            if len(self._rooms) >= MAX_TICKETS:
                stalest = min(self._rooms, key=lambda t: self._rooms[t].touched)
                del self._rooms[stalest]
            room = self._rooms[ticket] = _Room()
        room.touched = now
        return room

    def _sweep(self, now: float) -> None:
        viewer_cutoff = now - VIEWER_TTL_SECONDS
        event_cutoff = now - EVENT_TTL_SECONDS
        for ticket in list(self._rooms):
            room = self._rooms[ticket]
            for key in [k for k, v in room.viewers.items() if v.last_seen < viewer_cutoff]:
                del room.viewers[key]
            if room.events and room.events[0].at < event_cutoff:
                room.events = [e for e in room.events if e.at >= event_cutoff]
            if not room.viewers and not room.events:
                del self._rooms[ticket]


presence_hub = PresenceHub()
