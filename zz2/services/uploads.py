"""Media uploads: wiring diagrams, DIP tables and PDF manuals.

The monolith's upload endpoint accepted a base64 blob in a JSON body, checked that
the declared ``kind`` was one of four strings, appended ``.pdf`` if the filename
did not end in it, and wrote the bytes. Nothing verified that a file called
``manual.pdf`` was a PDF, or that ``wiring.jpg`` was an image.

That combination is stored cross-site scripting. Media is served from the same
origin as the application, so an "image" whose bytes are ``<script>`` can be
served as HTML — the browser sniffs, ignores the declared type, and executes it on
this origin with the victim's session in ``localStorage``.

Three defences here, and each closes a different half of the problem:

1. **Magic-byte validation.** The bytes must actually be the claimed format.
2. **Server-chosen filenames.** Images are always written as ``<kind>.jpg``, so a
   client-supplied name never reaches the filesystem.
3. **Per-file size limits.** Enforced on the decoded length, before writing.

The transport stays base64-in-JSON. Multipart would be more efficient, but the
Kit Editor's existing JavaScript posts base64 and changing both sides at once is a
larger, riskier change than the security fix requires. Noted in ``SECURITY.md`` as
follow-up work.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from zz2.core.errors import PayloadTooLarge, ValidationError
from zz2.core.events import MediaChanged, bus
from zz2.core.logging import audit
from zz2.core.text import safe_filename, safe_key
from zz2.paths import IMAGES_DIR, MANUALS_DIR
from zz2.settings import get_settings


class UploadKind(StrEnum):
    """What is being uploaded. Determines destination, format and filename."""

    MANUAL = "manual"
    DIP = "dip"
    WIRING = "wiring"
    MISC = "misc"

    @property
    def is_image(self) -> bool:
        return self is not UploadKind.MANUAL


@dataclass(frozen=True, slots=True)
class FormatSignature:
    """A file format identified by the bytes it must begin with."""

    name: str
    prefixes: tuple[bytes, ...]
    max_bytes: int

    def matches(self, data: bytes) -> bool:
        return any(data.startswith(prefix) for prefix in self.prefixes)


PDF = FormatSignature(
    name="PDF",
    # "%PDF-". A leading BOM or whitespace is not accepted: real PDF writers do
    # not emit one, and allowing slack is exactly how a sniffing bypass gets in.
    prefixes=(b"%PDF-",),
    max_bytes=25 * 1024 * 1024,
)

IMAGE = FormatSignature(
    name="JPEG, PNG, GIF or WebP",
    prefixes=(
        b"\xff\xd8\xff",          # JPEG
        b"\x89PNG\r\n\x1a\n",     # PNG
        b"GIF87a", b"GIF89a",     # GIF
        b"RIFF",                  # WebP container; "WEBP" checked at offset 8
    ),
    max_bytes=8 * 1024 * 1024,
)


@dataclass(frozen=True, slots=True)
class StoredUpload:
    """Where an accepted upload landed."""

    kind: UploadKind
    filename: str
    url_path: str | None

    def to_api(self) -> dict[str, object]:
        payload: dict[str, object] = {"ok": True, "kind": self.kind.value,
                                      "filename": self.filename}
        if self.url_path:
            payload["path"] = self.url_path
        return payload


class UploadService:
    """Validates and stores kit media."""

    def store(
        self,
        *,
        kit_key: str,
        kind: str,
        encoded: str,
        filename: str = "",
        actor: str,
    ) -> StoredUpload:
        """Decode, validate and write one upload.

        Args:
            kit_key: Destination kit. Becomes a directory name.
            kind: One of :class:`UploadKind`.
            encoded: Base64 payload, with or without a ``data:`` URL prefix.
            filename: Client-supplied name. Used for manuals only, sanitised.
            actor: Username, for the audit record.

        Raises:
            ValidationError: Bad key, unknown kind, undecodable payload, or bytes
                that are not the claimed format.
            PayloadTooLarge: Decoded size exceeds the limit for this format.
        """
        validated_key = safe_key(kit_key)
        if not validated_key:
            raise ValidationError("A valid kit key is required.")

        try:
            upload_kind = UploadKind(kind.lower().strip())
        except ValueError as exc:
            raise ValidationError(
                f"Unsupported upload type. Expected one of: "
                f"{', '.join(k.value for k in UploadKind)}."
            ) from exc

        data = self._decode(encoded)
        signature = IMAGE if upload_kind.is_image else PDF
        self._enforce_size(data, signature)
        self._enforce_format(data, signature)

        if upload_kind.is_image:
            stored = self._write_image(validated_key, upload_kind, data)
        else:
            stored = self._write_manual(validated_key, filename, data)

        audit("upload.stored", actor=actor, kit=validated_key,
              kind=upload_kind.value, bytes=len(data))
        bus.publish(MediaChanged(validated_key, upload_kind.value))
        return stored

    # -- Validation ------------------------------------------------------------
    @staticmethod
    def _decode(encoded: str) -> bytes:
        """Decode the base64 payload, stripping any ``data:`` URL prefix.

        ``validate=True`` rejects stray characters rather than skipping them.
        Lenient decoding lets a payload carry bytes the validator never sees,
        which would let a caller smuggle content past the magic-byte check.
        """
        if not encoded:
            raise ValidationError("The upload was empty.")

        payload = encoded
        if payload.startswith("data:") and "," in payload:
            payload = payload.split(",", 1)[1]

        try:
            data = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError("The upload could not be decoded.") from exc

        if not data:
            raise ValidationError("The upload was empty.")
        return data

    @staticmethod
    def _enforce_size(data: bytes, signature: FormatSignature) -> None:
        limit = min(signature.max_bytes, get_settings().max_upload_bytes)
        if len(data) > limit:
            raise PayloadTooLarge(
                f"That file is {len(data) // (1024 * 1024)} MB. "
                f"The limit is {limit // (1024 * 1024)} MB.",
                detail={"limit_bytes": limit, "size_bytes": len(data)},
            )

    @staticmethod
    def _enforce_format(data: bytes, signature: FormatSignature) -> None:
        """Verify the bytes are the claimed format. See the module docstring."""
        if not signature.matches(data):
            raise ValidationError(f"That file is not a valid {signature.name}.")

        # RIFF is a container family; only WebP is acceptable here. Without this
        # check a RIFF/AVI — or anything else starting with those four bytes —
        # would pass as an image.
        if data.startswith(b"RIFF") and data[8:12] != b"WEBP":
            raise ValidationError(f"That file is not a valid {signature.name}.")

    # -- Writing ---------------------------------------------------------------
    def _write_image(self, kit_key: str, kind: UploadKind, data: bytes) -> StoredUpload:
        """Write a diagram image under a server-chosen name.

        Always ``<kind>.jpg``, regardless of the real format or any name the
        client sent. The extension is a lie for a PNG upload, and harmlessly so:
        the tool references these by the fixed path, browsers render by content,
        and ``X-Content-Type-Options: nosniff`` plus an explicit ``Content-Type``
        on the media route stop the mismatch from mattering. What it buys is
        certainty that no client-supplied string ever becomes a filename.
        """
        directory = IMAGES_DIR / kit_key
        directory.mkdir(parents=True, exist_ok=True, mode=0o750)

        filename = f"{kind.value}.jpg"
        self._write_atomically(directory / filename, data)
        return StoredUpload(kind, filename, f"/images/{kit_key}/{filename}")

    def _write_manual(self, kit_key: str, filename: str, data: bytes) -> StoredUpload:
        """Write a PDF manual under a sanitised version of the given name.

        Manuals keep their names because the Kit Editor lists them to the user, so
        ``ITZ-GX1-A Installation.pdf`` is worth preserving. The name goes through
        :func:`~zz2.core.text.safe_filename` first, which reduces it to one path
        component of allowlisted characters.
        """
        directory = MANUALS_DIR / kit_key
        directory.mkdir(parents=True, exist_ok=True, mode=0o750)

        safe = safe_filename(filename or f"{kit_key}.pdf", fallback=f"{kit_key}.pdf")
        if not safe.lower().endswith(".pdf"):
            safe = f"{safe}.pdf"

        self._write_atomically(directory / safe, data)
        return StoredUpload(UploadKind.MANUAL, safe, None)

    @staticmethod
    def _write_atomically(path: Path, data: bytes) -> None:
        """Write via a temp file and rename, so a partial upload never replaces
        a good file. The monolith wrote directly to the destination: a connection
        dropped mid-write left a truncated diagram in place of the working one."""
        temp_path = path.with_name(f".{path.name}.part")
        try:
            temp_path.write_bytes(data)
            temp_path.chmod(0o640)
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)


upload_service = UploadService()
