"""Bounded time-to-live cache.

The monolith grew five hand-rolled caches — ``CSV_CACHE``,
``_CUSTOMER_HISTORY_CACHE``, ``_CUSTOMER_SUMMARIES_CACHE``, ``_ITEM_SALES_CACHE``
and ``_supabase_session_cache`` — each a bare dict with its own expiry
arithmetic. Only one of them ever evicted anything, and it did so with an
open-coded sweep that ran on insert. The rest grew without limit for the lifetime
of the process.

One implementation, used by all of them: TTL plus a hard capacity bound, so a
cache can never become the reason a long-running process runs out of memory.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """Thread-safe LRU cache whose entries also expire by age.

    Args:
        ttl_seconds: Maximum age of an entry before it is treated as absent.
        max_entries: Hard cap. The least recently used entry is evicted on
            overflow, which bounds memory regardless of key cardinality.
    """

    def __init__(self, ttl_seconds: float, max_entries: int = 512) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._ttl = ttl_seconds
        self._max = max_entries
        self._entries: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: K) -> V | None:
        """Return the live value for ``key``, or ``None`` if absent or stale."""
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= now:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def set(self, key: K, value: V) -> None:
        """Store ``value``, evicting the least recently used entry if full."""
        with self._lock:
            self._entries[key] = (time.monotonic() + self._ttl, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def get_or_set(self, key: K, factory: Callable[[], V]) -> V:
        """Return the cached value, computing and storing it on a miss.

        ``factory`` runs *outside* the lock. Two concurrent misses may therefore
        both compute — accepted deliberately, because the alternative is holding
        the lock across a network call and serialising every other reader behind
        it. Every current caller is idempotent and read-only.
        """
        cached = self.get(key)
        if cached is not None:
            return cached
        value = factory()
        self.set(key, value)
        return value

    def invalidate(self, key: K) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
