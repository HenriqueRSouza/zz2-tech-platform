"""Repository foundations.

The Repository pattern draws the line the monolith lacked: business logic asks a
repository for *domain objects* and never learns where they live. In the monolith
``_handle_kit_save`` opened ``kits.json``, mutated a dict, called ``_save_json``,
snapshotted history and triggered a Supabase upload — persistence, business rules
and remote sync in one method. Swapping JSON for Postgres would have meant
rewriting every handler.

Here a repository owns exactly one document or collection, and the storage
mechanism is behind :class:`JsonDocumentRepository`. Moving a collection to
Postgres means writing a new subclass with the same interface; no service, router
or worker changes.

Every repository also publishes a domain event after a successful write, so
mirroring and cache invalidation hook in without the persistence layer knowing
they exist.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generic, TypeVar

from zz2.core import jsonstore
from zz2.core.events import Event, bus

DocumentT = TypeVar("DocumentT")


class Repository(ABC):
    """Marker for the persistence boundary.

    Services depend on this abstraction, never on :mod:`zz2.core.jsonstore`. The
    rule is mechanical and worth stating: if a service imports ``jsonstore``, a
    storage change will reach into that service.
    """

    @abstractmethod
    def exists(self) -> bool:
        """Whether the backing store has been initialised."""


class JsonDocumentRepository(Repository, Generic[DocumentT]):
    """A repository backed by one atomically-replaced JSON document.

    Suits documents small enough to rewrite whole — kits, roles, blueprints,
    queues. The ticket store is a few megabytes and gets its own append-oriented
    subclass rather than paying a full rewrite per ticket.

    Args:
        path: Location of the document.
        default_factory: Produces the empty document. A *factory*, not a shared
            default value, so two repositories can never alias one mutable dict.
        change_event: Published after a successful write. ``None`` disables it.
    """

    def __init__(
        self,
        path: Path,
        default_factory: type[DocumentT] | Any,
        change_event: Event | None = None,
    ) -> None:
        self._path = path
        self._default_factory = default_factory
        self._change_event = change_event

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    def read(self) -> DocumentT:
        """Load the document, or a fresh empty one."""
        return jsonstore.read_json(self._path, self._default_factory())

    def write(self, document: DocumentT) -> None:
        """Replace the document atomically and announce the change."""
        jsonstore.write_json(self._path, document)
        self._announce()

    @contextmanager
    def mutate(self) -> Iterator[DocumentT]:
        """Read-modify-write under the document's lock.

        The yielded value is written back on normal exit. An exception inside the
        block leaves the stored document untouched.
        """
        with jsonstore.mutate(self._path, self._default_factory()) as document:
            yield document
        self._announce()

    def mtime(self) -> float | None:
        return jsonstore.file_mtime(self._path)

    def _announce(self) -> None:
        if self._change_event is not None:
            bus.publish(self._change_event)
