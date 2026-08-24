"""Supabase Storage gateway for private kit manuals.

PDFs live in a private bucket and are accessed only with the server-side service
key.  Browser requests still pass through the application's authenticated
``/manuals/<kit>/<file>`` route, so neither the bucket nor its credentials need
to be public.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from zz2.core.errors import NotConfigured, NotFound, UpstreamError
from zz2.gateways.http import get_client
from zz2.settings import get_settings

_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")


@dataclass(frozen=True, slots=True)
class StorageDownload:
    """An object body plus the HTTP metadata needed for PDF range requests."""

    content: bytes
    status_code: int
    headers: dict[str, str]


class SupabaseStorageGateway:
    """Read and write kit manuals in a private Supabase Storage bucket."""

    def __init__(self) -> None:
        self._bucket_ready = False
        self._bucket_lock = threading.Lock()

    @property
    def configured(self) -> bool:
        settings = get_settings()
        return bool(
            settings.enable_supabase_storage
            and settings.supabase_url
            and settings.supabase_service_key.get_secret_value()
            and settings.supabase_storage_bucket
        )

    def _configuration(self) -> tuple[str, str, str]:
        settings = get_settings()
        key = settings.supabase_service_key.get_secret_value()
        bucket = settings.supabase_storage_bucket.strip()
        if not (
            settings.enable_supabase_storage
            and settings.supabase_url
            and key
            and _BUCKET_RE.fullmatch(bucket)
        ):
            raise NotConfigured("Supabase Storage is not configured on this server.")
        return settings.supabase_storage_url, key, bucket

    @staticmethod
    def _headers(key: str) -> dict[str, str]:
        return {"apikey": key, "Authorization": f"Bearer {key}"}

    @staticmethod
    def _object_path(kit_key: str, filename: str) -> str:
        return f"{quote(kit_key, safe='')}/{quote(filename, safe='')}"

    @staticmethod
    def _error_code(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except (AttributeError, ValueError):
            return ""
        return str(payload.get("code") or "") if isinstance(payload, dict) else ""

    def ensure_bucket(self) -> None:
        """Create the private PDF-only bucket once, if it does not exist."""
        if self._bucket_ready:
            return
        with self._bucket_lock:
            if self._bucket_ready:
                return
            base, key, bucket = self._configuration()
            headers = self._headers(key)
            try:
                response = get_client().get(base + f"bucket/{quote(bucket, safe='')}",
                                            headers=headers)
                if response.status_code == 404 or self._error_code(response) == "NoSuchBucket":
                    response = get_client().post(
                        base + "bucket",
                        headers=headers,
                        json={
                            "id": bucket,
                            "name": bucket,
                            "public": False,
                            "file_size_limit": get_settings().max_upload_bytes,
                            "allowed_mime_types": ["application/pdf"],
                        },
                    )
            except httpx.HTTPError as exc:
                raise UpstreamError(
                    log_detail=f"supabase storage bucket transport: {exc}"
                ) from exc
            if response.status_code >= 400:
                raise UpstreamError(
                    log_detail=(
                        "supabase storage bucket HTTP "
                        f"{response.status_code}: {response.text[:500]}"
                    )
                )
            self._bucket_ready = True

    def upload_manual(self, kit_key: str, filename: str, data: bytes) -> None:
        """Upload or replace a validated PDF."""
        self.ensure_bucket()
        base, key, bucket = self._configuration()
        path = self._object_path(kit_key, filename)
        headers = {
            **self._headers(key),
            "Content-Type": "application/pdf",
            "cache-control": "max-age=300",
            "x-upsert": "true",
        }
        try:
            response = get_client().post(
                base + f"object/{quote(bucket, safe='')}/{path}",
                headers=headers,
                content=data,
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                log_detail=f"supabase storage upload transport: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise UpstreamError(
                "The manual could not be stored. Try again shortly.",
                log_detail=(
                    f"supabase storage upload HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                ),
            )

    def download_manual(
        self,
        kit_key: str,
        filename: str,
        *,
        range_header: str = "",
    ) -> StorageDownload:
        """Download a private PDF, forwarding an optional HTTP byte range."""
        base, key, bucket = self._configuration()
        path = self._object_path(kit_key, filename)
        headers = self._headers(key)
        if range_header:
            headers["Range"] = range_header
        try:
            response = get_client().get(
                base + f"object/authenticated/{quote(bucket, safe='')}/{path}",
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                log_detail=f"supabase storage download transport: {exc}"
            ) from exc
        if response.status_code == 404 or self._error_code(response) in {
            "NoSuchKey", "NoSuchBucket"
        }:
            raise NotFound("Manual not found.")
        if response.status_code not in {200, 206}:
            raise UpstreamError(
                log_detail=(
                    f"supabase storage download HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )
            )

        forwarded = {}
        for name in ("content-length", "content-range", "etag", "last-modified"):
            value = response.headers.get(name)
            if value:
                forwarded[name.title()] = value
        return StorageDownload(response.content, response.status_code, forwarded)

    def copy_manual(self, source_kit: str, target_kit: str, filename: str) -> None:
        """Copy a manual when a kit is duplicated."""
        self.ensure_bucket()
        base, key, bucket = self._configuration()
        try:
            response = get_client().post(
                base + "object/copy",
                headers=self._headers(key),
                json={
                    "bucketId": bucket,
                    "sourceKey": f"{source_kit}/{filename}",
                    "destinationKey": f"{target_kit}/{filename}",
                },
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                log_detail=f"supabase storage copy transport: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise UpstreamError(
                log_detail=(
                    f"supabase storage copy HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )
            )

    def delete_manual(self, kit_key: str, filename: str) -> None:
        """Delete one object; used by migrations and smoke-test cleanup."""
        base, key, bucket = self._configuration()
        try:
            response = get_client().request(
                "DELETE",
                base + f"object/{quote(bucket, safe='')}",
                headers=self._headers(key),
                json={"prefixes": [f"{kit_key}/{filename}"]},
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                log_detail=f"supabase storage delete transport: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise UpstreamError(
                log_detail=(
                    f"supabase storage delete HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )
            )


storage_gateway = SupabaseStorageGateway()
