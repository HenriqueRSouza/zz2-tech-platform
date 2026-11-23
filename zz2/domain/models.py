"""Domain entities.

Pydantic models rather than the bare dicts the monolith passed around. The dicts
were the source of a specific recurring bug class: the same ticket appeared as
``{'ticket': ...}`` in the local store, ``{'ticketNum': ...}`` in the tool's API
responses and ``{'ticket_number': ...}`` in the database, and every function that
touched one had to guess which shape it had received. ``_pick(o, ['ticket',
'ticket_no', 'Ticket #', 'number'])`` exists because of it.

The mapping between representations is declared once, here, in the ``from_*`` and
``to_*`` methods. Everything upstream works with a typed :class:`Ticket`.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zz2.core.text import as_text, digits_only


class TicketStatus(StrEnum):
    """Canonical status buckets used by analytics.

    Free text arrives from a Google Sheet where humans typed it, so the raw value
    is preserved alongside this bucket; :meth:`canonicalise` collapses the
    variants ("Resolved", "resolved ", "RESOLVED - see notes") into one.
    """

    OPEN = "open"
    RESOLVED = "resolved"
    NO_SOLUTION = "nosol"
    RMA = "rma"
    DESTROY = "destroy"
    DISTRIBUTOR = "distrib"
    WOE = "woe"
    OTHER = "other"

    @classmethod
    def canonicalise(cls, raw: object) -> TicketStatus:
        """Map free-text status to a bucket.

        Substring matching, in a deliberate order — "waiting for customer
        feedback" must land on :attr:`OPEN` before the generic fallback, and the
        checks are ordered so the more specific pattern wins.

        Examples:
            >>> TicketStatus.canonicalise("Waiting for customer feedback")
            <TicketStatus.OPEN: 'open'>
            >>> TicketStatus.canonicalise("RMA issued")
            <TicketStatus.RMA: 'rma'>
            >>> TicketStatus.canonicalise("")
            <TicketStatus.OPEN: 'open'>
        """
        text = as_text(raw).strip().lower()
        if not text:
            return cls.OPEN
        if "resolv" in text:
            return cls.RESOLVED
        if "wait" in text:
            return cls.OPEN
        if "no sol" in text or "nosol" in text:
            return cls.NO_SOLUTION
        if "rma" in text:
            return cls.RMA
        if "destroy" in text:
            return cls.DESTROY
        if "distrib" in text:
            return cls.DISTRIBUTOR
        if text in {"w.o.e.", "woe"} or "w.o.e" in text:
            return cls.WOE
        return cls.OTHER


class Ticket(BaseModel):
    """A support ticket, in the application's own vocabulary.

    ``number`` holds whatever the source system uses (``"7388"`` or
    ``"TKT-7388"``); :attr:`numeric` is the comparable form.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    number: str = ""
    date: str = ""
    tech: str = ""
    client: str = ""
    product: str = ""
    vehicle: str = ""
    issue: str = ""
    resolution: str = ""
    status: str = ""
    serial: str = ""
    notes: str = ""

    @property
    def numeric(self) -> int | None:
        """Ticket number as an integer, or ``None`` when it has no digits."""
        digits = digits_only(self.number)
        return int(digits) if digits else None

    @property
    def canonical_number(self) -> str:
        """``TKT-nnnn`` — the form stored in the database."""
        digits = digits_only(self.number)
        return f"TKT-{digits}" if digits else as_text(self.number).strip()

    @property
    def status_bucket(self) -> TicketStatus:
        return TicketStatus.canonicalise(self.status)

    # -- Representations -------------------------------------------------------
    @classmethod
    def from_store(cls, row: dict[str, Any]) -> Self:
        """Read a row of the local ``analytics.json`` store.

        Column names vary across generations of the sheet export, so each field
        is resolved against a list of aliases — the behaviour of the monolith's
        ``_pick`` helper, kept because the historical data really is that
        inconsistent.
        """
        return cls(
            number=as_text(_first(row, "ticket", "ticketNum", "ticket_no", "Ticket #", "number")),
            date=as_text(_first(row, "date", "Date", "DATE")),
            # strip() only, never whitespace collapsing. The analytics pipeline
            # records the raw label in its merge report, and collapsing here would
            # show the user a value they never typed. Internal whitespace is
            # normalised later, inside the consolidation itself.
            tech=as_text(_first(row, "tech", "technician", "Tech")).strip(),
            client=as_text(_first(row, "client", "customer", "Client")).strip(),
            product=as_text(_first(row, "product", "kit", "Product", "Kit")).strip(),
            vehicle=as_text(_first(row, "vehicle", "Vehicle")).strip(),
            issue=as_text(_first(row, "issue", "Issue")).strip(),
            resolution=as_text(_first(row, "resolution", "Resolution")).strip(),
            status=as_text(_first(row, "status", "Status")).strip(),
            serial=as_text(_first(row, "serial", "Serial")).strip(),
            notes=as_text(_first(row, "notes", "Notes")).strip(),
        )

    @classmethod
    def from_record(cls, row: dict[str, Any]) -> Self:
        """Read a ``techSupport.tickets`` row (dates already ISO strings)."""
        return cls(
            number=as_text(row.get("ticket_number")),
            date=as_text(row.get("ticket_date")),
            tech=as_text(row.get("tech")),
            client=as_text(row.get("client")),
            product=as_text(row.get("product")),
            vehicle=as_text(row.get("vehicle")),
            issue=as_text(row.get("issue")),
            resolution=as_text(row.get("resolution")),
            status=as_text(row.get("status")),
            serial=as_text(row.get("serial")),
            notes=as_text(row.get("notes")),
        )

    def to_store(self) -> dict[str, Any]:
        """The shape written to the local store."""
        return {
            "date": self.date,
            "ticket": self.number,
            "tech": self.tech,
            "client": self.client,
            "product": self.product,
            "vehicle": self.vehicle,
            "issue": self.issue,
            "resolution": self.resolution,
            "status": self.status,
            "serial": self.serial,
            "notes": self.notes,
        }

    def to_api(self) -> dict[str, Any]:
        """The shape the existing pages consume (``ticketNum``, not ``ticket``)."""
        return {
            "ticketNum": self.number,
            "date": self.date,
            "tech": self.tech,
            "client": self.client,
            "product": self.product,
            "vehicle": self.vehicle,
            "issue": self.issue,
            "resolution": self.resolution,
            "status": self.status,
            "serial": self.serial,
            "notes": self.notes,
        }

    def to_record(self, *, include_identity: bool = True) -> dict[str, Any]:
        """The shape written to ``techSupport.tickets``.

        Args:
            include_identity: When false, ``ticket_date`` and ``ticket_number``
                are omitted. An update must not rewrite either — the number is
                the row's identity and the date records creation, not last edit.
        """
        record: dict[str, Any] = {
            "tech": self.tech or None,
            "client": self.client or None,
            "product": self.product or None,
            "vehicle": self.vehicle or None,
            "issue": self.issue or None,
            "resolution": self.resolution or None,
            "status": self.status or None,
            "serial": self.serial or None,
            "notes": self.notes or None,
        }
        if include_identity:
            record["ticket_number"] = self.canonical_number
            record["ticket_date"] = self.date or dt.date.today().isoformat()
        return record


def _first(row: dict[str, Any], *names: str) -> Any:
    """First non-empty value among ``names``."""
    for name in names:
        value = row.get(name)
        if value is not None and value != "":
            return value
    return ""


class TroubleshootingNode(BaseModel):
    """One node of a kit's diagnostic tree.

    The tree is stored flat and linked by *text*: a node's ``parent`` holds the
    ``issue`` string of its parent, not an id. Fragile — renaming an issue orphans
    its children — but it is the on-disk format the Kit Editor writes and the
    tool renders, so it is preserved rather than migrated.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="allow")

    parent: str = ""
    issue: str = ""
    question: str = ""
    steps: list[str] = Field(default_factory=list)

    @field_validator("steps", mode="before")
    @classmethod
    def _coerce_steps(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, list | tuple):
            return [as_text(item) for item in value if as_text(item).strip()]
        return []


class Kit(BaseModel):
    """Kit metadata as stored in ``store/kits.json`` under its key."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="allow")

    vehicle: str = ""
    switch_button: str = ""
    audio_method: str = ""
    notes: str = ""
    dip_image: str = ""
    wiring_image: str = ""
    misc_image: str = ""
    manual: str = ""
    updated_at: int | None = None


class Blueprint(BaseModel):
    """A reusable troubleshooting tree, applied to kits from the Kit Editor."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="allow")

    name: str = ""
    troubleshooting: list[TroubleshootingNode] = Field(default_factory=list)
    created_at: int | None = None
    updated_at: int | None = None


class WriteOperation(BaseModel):
    """A queued, retryable write to the Google Apps Script sheet.

    The Command pattern, made durable: the operation is serialised to
    ``pending_writes.json`` so it survives a restart, then replayed by the
    write-behind worker until it succeeds or exhausts its attempts.
    """

    id: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    predicted_number: int | None = None
    attempts: int = 0
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise using the on-disk key names of the existing queue file."""
        return {
            "id": self.id,
            "action": self.action,
            "params": self.params,
            "predicted_num": self.predicted_number,
            "attempts": self.attempts,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            id=as_text(raw.get("id")),
            action=as_text(raw.get("action")),
            params=raw.get("params") or {},
            predicted_number=raw.get("predicted_num"),
            attempts=int(raw.get("attempts") or 0),
            created_at=as_text(raw.get("created_at")),
        )
