"""Azure Blob Storage gateway for private kit media.

Diagrams and PDFs live in a private container (``<kit_key>/<filename>``) and
are accessed only with the server-side connection string. Browser requests
still pass through the authenticated ``/images/<kit>/<file>`` and
``/manuals/<kit>/<file>`` routes, so neither the container nor its credentials
need to be public.

The SDK client is built lazily on first use and rebuilt only when the
connection string changes. The connection string is never logged: error
details carry the Azure error code and status, not the SDK's request URL or
the credentials.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from email.utils import format_datetime
from typing import Any

from zz2.core.errors import NotConfigured, NotFound, UpstreamError
from zz2.settings import get_settings

# Azure container names: 3-63 chars, lowercase letters, digits and single
# hyphens, starting and ending with a letter or digit.
_CONTAINER_RE = re.compile(r"^(?=.{3,63}$)[a-z0-9]+(-[a-z0-9]+)*$")
_RANGE_RE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE)

MEDIA_CACHE_CONTROL = "max-age=300"


@dataclass(frozen=True, slots=True)
class StorageDownload:
    """An object body plus the HTTP metadata needed by the media response."""

    content: bytes
    status_code: int
    headers: dict[str, str]


def _describe(exc: BaseException) -> str:
    """A log-safe description of an Azure SDK error (no URL, no credentials)."""
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "error_code", None)
    parts = [type(exc).__name__]
    if status:
        parts.append(f"HTTP {status}")
    if code:
        parts.append(str(code))
    return " ".join(parts)


def _parse_range(range_header: str) -> tuple[int | None, int | None, int | None] | None:
    """Parse a single ``bytes=`` range.

    Returns ``(start, end, suffix_length)`` or ``None`` when the header is
    absent or not a single well-formed byte range (the full object is served
    then, which RFC 9110 allows).
    """
    if not range_header:
        return None
    match = _RANGE_RE.match(range_header)
    if not match:
        return None
    first, last = match.groups()
    if first:
        start = int(first)
        end = int(last) if last else None
        if end is not None and end < start:
            return None
        return start, end, None
    if last:
        suffix = int(last)
        return (None, None, suffix) if suffix > 0 else None
    return None


def _total_from_content_range(value: str | None) -> int | None:
    if not value or "/" not in value:
        return None
    total = value.rsplit("/", 1)[1].strip()
    return int(total) if total.isdigit() else None


class AzureBlobStorageGateway:
    """Read and write kit media in a private Azure Blob Storage container."""

    def __init__(self, service_client: Any | None = None) -> None:
        self._container_ready = False
        self._lock = threading.Lock()
        # An injected client (tests) wins over the lazily built SDK client.
        self._injected = service_client
        self._client: Any | None = None
        self._client_fingerprint = ""

    # -- configuration ---------------------------------------------------------

    @property
    def configured(self) -> bool:
        settings = get_settings()
        return bool(
            settings.enable_blob_storage
            and settings.azure_storage_connection_string.get_secret_value()
            and settings.azure_storage_container
        )

    def _container_name(self) -> str:
        settings = get_settings()
        connection_string = settings.azure_storage_connection_string.get_secret_value()
        container = settings.azure_storage_container.strip()
        if not (
            settings.enable_blob_storage
            and connection_string
            and _CONTAINER_RE.fullmatch(container)
        ):
            raise NotConfigured("Azure Blob Storage is not configured on this server.")
        return container

    def _service(self) -> Any:
        if self._injected is not None:
            return self._injected
        connection_string = get_settings().azure_storage_connection_string.get_secret_value()
        fingerprint = hashlib.sha256(connection_string.encode()).hexdigest()
        with self._lock:
            if self._client is None or fingerprint != self._client_fingerprint:
                from azure.storage.blob import BlobServiceClient

                try:
                    self._client = BlobServiceClient.from_connection_string(connection_string)
                except ValueError as exc:
                    # The SDK's message can echo parts of the string; drop it.
                    raise NotConfigured(
                        "Azure Blob Storage is not configured on this server.",
                        log_detail="azure storage connection string is malformed",
                    ) from exc
                self._client_fingerprint = fingerprint
                self._container_ready = False
            return self._client

    def _container(self) -> Any:
        container = self._container_name()
        return self._service().get_container_client(container)

    def _blob(self, kit_key: str, filename: str) -> Any:
        return self._container().get_blob_client(self._object_path(kit_key, filename))

    @staticmethod
    def _object_path(kit_key: str, filename: str) -> str:
        # Blob names are stored unencoded; the SDK URL-encodes them on the wire.
        return f"{kit_key}/{filename}"

    # -- container -------------------------------------------------------------

    def ensure_container(self) -> None:
        """Create the private kit-media container once, if it does not exist."""
        if self._container_ready:
            return
        from azure.core.exceptions import AzureError, ResourceExistsError

        container = self._container()
        with self._lock:
            if self._container_ready:
                return
            try:
                # No ``public_access`` argument: the container stays private.
                container.create_container()
            except ResourceExistsError:
                pass
            except AzureError as exc:
                raise UpstreamError(
                    log_detail=f"azure blob container create: {_describe(exc)}"
                ) from exc
            self._container_ready = True

    # Kept for callers written against the Supabase gateway.
    ensure_bucket = ensure_container

    # -- writes ----------------------------------------------------------------

    def upload_manual(self, kit_key: str, filename: str, data: bytes) -> None:
        """Upload or replace a validated PDF."""
        self._upload_object(kit_key, filename, data, content_type="application/pdf")

    def upload_image(
        self,
        kit_key: str,
        filename: str,
        data: bytes,
        *,
        content_type: str = "image/jpeg",
    ) -> None:
        """Upload or replace a validated diagram image."""
        self._upload_object(kit_key, filename, data, content_type=content_type)

    def _upload_object(
        self,
        kit_key: str,
        filename: str,
        data: bytes,
        *,
        content_type: str,
    ) -> None:
        from azure.core.exceptions import AzureError
        from azure.storage.blob import ContentSettings

        self.ensure_container()
        blob = self._blob(kit_key, filename)
        try:
            blob.upload_blob(
                data,
                overwrite=True,
                content_settings=ContentSettings(
                    content_type=content_type, cache_control=MEDIA_CACHE_CONTROL
                ),
            )
        except AzureError as exc:
            raise UpstreamError(
                "The file could not be stored. Try again shortly.",
                log_detail=f"azure blob upload: {_describe(exc)}",
            ) from exc

    def copy_manual(self, source_kit: str, target_kit: str, filename: str) -> None:
        """Copy a manual when a kit is duplicated.

        Kit media is small, so the object is read and written back rather than
        using an asynchronous server-side copy that may still be pending when
        the duplicated kit is first opened.
        """
        from azure.core.exceptions import AzureError, ResourceNotFoundError
        from azure.storage.blob import ContentSettings

        self.ensure_container()
        source = self._blob(source_kit, filename)
        target = self._blob(target_kit, filename)
        try:
            downloader = source.download_blob()
            data = downloader.readall()
            settings = getattr(downloader.properties, "content_settings", None)
            content_type = getattr(settings, "content_type", None) or "application/pdf"
            target.upload_blob(
                data,
                overwrite=True,
                content_settings=ContentSettings(
                    content_type=content_type, cache_control=MEDIA_CACHE_CONTROL
                ),
            )
        except ResourceNotFoundError as exc:
            raise NotFound("Media file not found.") from exc
        except AzureError as exc:
            raise UpstreamError(
                log_detail=f"azure blob copy: {_describe(exc)}"
            ) from exc

    def delete_manual(self, kit_key: str, filename: str) -> None:
        """Delete one object; used by migrations and smoke-test cleanup.

        Deleting an object that does not exist is not an error.
        """
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        blob = self._blob(kit_key, filename)
        try:
            blob.delete_blob()
        except ResourceNotFoundError:
            return
        except AzureError as exc:
            raise UpstreamError(
                log_detail=f"azure blob delete: {_describe(exc)}"
            ) from exc

    # -- reads -----------------------------------------------------------------

    def download_manual(
        self,
        kit_key: str,
        filename: str,
        *,
        range_header: str = "",
    ) -> StorageDownload:
        """Download a private PDF, honouring an optional HTTP byte range."""
        return self.download_object(kit_key, filename, range_header=range_header)

    def download_image(self, kit_key: str, filename: str) -> StorageDownload:
        """Download a private diagram image."""
        return self.download_object(kit_key, filename)

    def download_object(
        self,
        kit_key: str,
        filename: str,
        *,
        range_header: str = "",
    ) -> StorageDownload:
        """Download one private kit object, honouring an optional byte range.

        Returns ``200`` with the whole body, ``206`` with ``Content-Range`` for a
        satisfiable range, or ``416`` with ``Content-Range: bytes */<size>``.
        """
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        blob = self._blob(kit_key, filename)
        requested = _parse_range(range_header)
        first = 0
        try:
            if requested is None:
                downloader = blob.download_blob()
                partial = False
            else:
                start, end, suffix = requested
                if suffix is not None:
                    size = int(blob.get_blob_properties().size or 0)
                    if size == 0:
                        return self._unsatisfiable(0)
                    start = max(size - suffix, 0)
                    end = size - 1
                assert start is not None
                first = start
                length = None if end is None else end - start + 1
                downloader = blob.download_blob(offset=start, length=length)
                partial = True
            content = downloader.readall()
        except ResourceNotFoundError as exc:
            raise NotFound("Media file not found.") from exc
        except AzureError as exc:
            if requested is not None and getattr(exc, "status_code", None) == 416:
                total = self._safe_size(blob)
                return self._unsatisfiable(total)
            raise UpstreamError(
                log_detail=f"azure blob download: {_describe(exc)}"
            ) from exc

        properties = downloader.properties
        headers: dict[str, str] = {"Content-Length": str(len(content))}
        etag = getattr(properties, "etag", None)
        if etag:
            headers["Etag"] = str(etag)
        last_modified = getattr(properties, "last_modified", None)
        if last_modified is not None:
            headers["Last-Modified"] = format_datetime(last_modified, usegmt=True)

        if not partial:
            return StorageDownload(content, 200, headers)

        total = _total_from_content_range(getattr(properties, "content_range", None))
        if total is None:
            total = self._safe_size(blob)
        headers["Content-Range"] = (
            f"bytes {first}-{first + len(content) - 1}/{total}"
            if content
            else f"bytes */{total}"
        )
        return StorageDownload(content, 206 if content else 416, headers)

    @staticmethod
    def _unsatisfiable(total: int) -> StorageDownload:
        return StorageDownload(b"", 416, {"Content-Range": f"bytes */{total}"})

    @staticmethod
    def _safe_size(blob: Any) -> int:
        from azure.core.exceptions import AzureError

        try:
            return int(blob.get_blob_properties().size or 0)
        except AzureError:
            return 0


# Backwards-compatible alias for imports written against the old gateway name.
StorageGateway = AzureBlobStorageGateway

storage_gateway = AzureBlobStorageGateway()
