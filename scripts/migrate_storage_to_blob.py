#!/usr/bin/env python3
"""Copy every object from the Supabase Storage bucket to the Azure Blob container.

Object names are preserved (``<kit_key>/<filename>``), so the authenticated
``/images`` and ``/manuals`` routes resolve the same paths after the switch.
Existing blobs are overwritten, which makes the copy idempotent and safe to
re-run. The content type recorded by Supabase is preserved.

Environment:

    SUPABASE_URL                         https://<project>.supabase.co
    SUPABASE_SERVICE_KEY                 service-role / secret key
    SUPABASE_BUCKET                      default: zz2-manuals
    ZZ2_AZURE_STORAGE_CONNECTION_STRING  storage account connection string
    ZZ2_AZURE_STORAGE_CONTAINER          default: zz2-manuals

Usage:

    python scripts/migrate_storage_to_blob.py --dry-run
    python scripts/migrate_storage_to_blob.py

Credentials are never printed.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

PAGE_SIZE = 1000
DEFAULT_BUCKET = "zz2-manuals"
DEFAULT_CONTAINER = "zz2-manuals"
CACHE_CONTROL = "max-age=300"


@dataclass(frozen=True, slots=True)
class SourceObject:
    path: str
    size: int | None
    content_type: str | None


@dataclass(slots=True)
class Summary:
    copied: int = 0
    bytes: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


class SupabaseStorageSource:
    """Minimal read-only client for the Supabase Storage REST API."""

    def __init__(self, url: str, key: str, bucket: str, client: Any | None = None) -> None:
        self.base = url.rstrip("/") + "/storage/v1/"
        self.bucket = bucket
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        self.client = client if client is not None else httpx.Client(timeout=60.0)

    def _list_page(self, prefix: str, offset: int) -> list[dict[str, Any]]:
        response = self.client.post(
            self.base + f"object/list/{quote(self.bucket, safe='')}",
            headers=self.headers,
            json={
                "prefix": prefix,
                "limit": PAGE_SIZE,
                "offset": offset,
                "sortBy": {"column": "name", "order": "asc"},
            },
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"list {prefix or '/'} failed: HTTP {response.status_code} {response.text[:300]}"
            )
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError(f"list {prefix or '/'} returned an unexpected payload")
        return payload

    def iter_objects(self, prefix: str = "") -> Iterator[SourceObject]:
        """Yield every object under ``prefix``, recursing into folders.

        Supabase returns folders as entries whose ``id`` is ``null``.
        """
        offset = 0
        while True:
            page = self._list_page(prefix, offset)
            for entry in page:
                name = entry.get("name")
                if not name or name == ".emptyFolderPlaceholder":
                    continue
                path = f"{prefix}/{name}" if prefix else name
                if entry.get("id") is None:
                    yield from self.iter_objects(path)
                    continue
                metadata = entry.get("metadata") or {}
                size = metadata.get("size") if isinstance(metadata, dict) else None
                mimetype = metadata.get("mimetype") if isinstance(metadata, dict) else None
                yield SourceObject(
                    path=path,
                    size=int(size) if isinstance(size, int | float) else None,
                    content_type=mimetype or None,
                )
            if len(page) < PAGE_SIZE:
                return
            offset += PAGE_SIZE

    def download(self, path: str) -> tuple[bytes, str | None]:
        encoded = "/".join(quote(part, safe="") for part in path.split("/"))
        response = self.client.get(
            self.base + f"object/{quote(self.bucket, safe='')}/{encoded}",
            headers=self.headers,
        )
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code} {response.text[:300]}")
        return response.content, response.headers.get("content-type")


def _container_client(connection_string: str, container: str) -> Any:
    from azure.core.exceptions import ResourceExistsError
    from azure.storage.blob import BlobServiceClient

    service = BlobServiceClient.from_connection_string(connection_string)
    client = service.get_container_client(container)
    try:
        client.create_container()  # private: no public_access
    except ResourceExistsError:
        pass
    return client


def migrate(
    source: SupabaseStorageSource,
    container: Any | None,
    *,
    dry_run: bool = False,
    out: Any = sys.stdout,
) -> Summary:
    """Copy all objects from ``source`` into ``container`` (an Azure ContainerClient)."""
    summary = Summary()
    for obj in source.iter_objects():
        if dry_run:
            print(f"would copy {obj.path} ({obj.size or 0} bytes, "
                  f"{obj.content_type or 'unknown type'})", file=out)
            summary.copied += 1
            summary.bytes += obj.size or 0
            continue
        try:
            data, header_type = source.download(obj.path)
            content_type = obj.content_type or header_type or "application/octet-stream"
            from azure.storage.blob import ContentSettings

            container.upload_blob(
                obj.path,
                data,
                overwrite=True,
                content_settings=ContentSettings(
                    content_type=content_type, cache_control=CACHE_CONTROL
                ),
            )
        except Exception as exc:  # noqa: BLE001 - keep going, report at the end
            reason = f"{type(exc).__name__}: {str(exc)[:300]}"
            summary.failures.append((obj.path, reason))
            print(f"FAILED {obj.path}: {reason}", file=out)
            continue
        summary.copied += 1
        summary.bytes += len(data)
        print(f"copied {obj.path} ({len(data)} bytes, {content_type})", file=out)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be copied without writing to Azure")
    args = parser.parse_args(argv)

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    bucket = os.environ.get("SUPABASE_BUCKET", "").strip() or DEFAULT_BUCKET
    connection_string = os.environ.get("ZZ2_AZURE_STORAGE_CONNECTION_STRING", "").strip()
    container_name = (
        os.environ.get("ZZ2_AZURE_STORAGE_CONTAINER", "").strip() or DEFAULT_CONTAINER
    )

    missing = [name for name, value in (
        ("SUPABASE_URL", url),
        ("SUPABASE_SERVICE_KEY", key),
        ("ZZ2_AZURE_STORAGE_CONNECTION_STRING", connection_string if not args.dry_run else "-"),
    ) if not value]
    if missing:
        print(f"missing environment variables: {', '.join(missing)}", file=sys.stderr)
        return 2

    source = SupabaseStorageSource(url, key, bucket)
    container = None
    if not args.dry_run:
        try:
            container = _container_client(connection_string, container_name)
        except Exception as exc:  # noqa: BLE001
            # Do not echo the SDK message: it can include parts of the string.
            print(f"could not open Azure container {container_name!r}: "
                  f"{type(exc).__name__}", file=sys.stderr)
            return 1

    print(f"source: supabase bucket {bucket!r} -> azure container {container_name!r}"
          f"{' (dry run)' if args.dry_run else ''}")
    try:
        summary = migrate(source, container, dry_run=args.dry_run)
    except RuntimeError as exc:
        print(f"listing failed: {exc}", file=sys.stderr)
        return 1

    verb = "would copy" if args.dry_run else "copied"
    print(f"\n{verb}: {summary.copied} objects, {summary.bytes} bytes; "
          f"failures: {len(summary.failures)}")
    for path, reason in summary.failures:
        print(f"  {path}: {reason}")
    return 1 if summary.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
