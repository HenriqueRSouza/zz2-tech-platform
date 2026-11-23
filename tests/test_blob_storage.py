"""Private Azure Blob Storage integration for kit manuals and diagrams."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
    ServiceRequestError,
)

from scripts.migrate_manuals_to_storage import (
    choose_primary,
    destination_name,
    discover_manuals,
)
from scripts.migrate_storage_to_blob import SupabaseStorageSource, migrate
from tests.conftest import auth_header
from zz2.core.errors import NotConfigured, NotFound, UpstreamError
from zz2.gateways.storage import (
    AzureBlobStorageGateway,
    StorageDownload,
    storage_gateway,
)

CONNECTION_STRING = (
    "DefaultEndpointsProtocol=https;AccountName=zz2test;"
    "AccountKey=c2VjcmV0LWFjY291bnQta2V5;EndpointSuffix=core.windows.net"
)
MODIFIED = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


# -- fake Azure SDK -------------------------------------------------------------


class FakeDownloader:
    def __init__(self, data: bytes, total: int, offset: int, settings: Any) -> None:
        self._data = data
        end = offset + len(data) - 1
        self.properties = SimpleNamespace(
            etag='"0x8DDBLOB"',
            last_modified=MODIFIED,
            content_range=f"bytes {offset}-{end}/{total}",
            content_settings=settings,
            size=len(data),
        )

    def readall(self) -> bytes:
        return self._data


class FakeBlob:
    def __init__(self, container: FakeContainer, name: str) -> None:
        self.container = container
        self.name = name

    def upload_blob(self, data: bytes, *, overwrite: bool = False, content_settings=None) -> None:
        self.container.calls.append(("upload", self.name, overwrite))
        if self.container.fail_with is not None:
            raise self.container.fail_with
        if self.name in self.container.blobs and not overwrite:
            raise ResourceExistsError("exists")
        self.container.blobs[self.name] = (bytes(data), content_settings)

    def _stored(self) -> tuple[bytes, Any]:
        if self.container.fail_with is not None:
            raise self.container.fail_with
        if self.name not in self.container.blobs:
            raise ResourceNotFoundError("The specified blob does not exist.")
        return self.container.blobs[self.name]

    def download_blob(self, offset: int | None = None, length: int | None = None):
        self.container.calls.append(("download", self.name, offset, length))
        data, settings = self._stored()
        start = offset or 0
        if offset is not None and start >= len(data):
            error = HttpResponseError("The range specified is invalid.")
            error.status_code = 416
            raise error
        end = len(data) if length is None else min(len(data), start + length)
        return FakeDownloader(data[start:end], len(data), start, settings)

    def get_blob_properties(self) -> SimpleNamespace:
        data, _settings = self._stored()
        return SimpleNamespace(size=len(data))

    def delete_blob(self) -> None:
        self.container.calls.append(("delete", self.name))
        self._stored()
        del self.container.blobs[self.name]


class FakeContainer:
    def __init__(self, name: str) -> None:
        self.name = name
        self.blobs: dict[str, tuple[bytes, Any]] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.exists = False
        self.fail_with: Exception | None = None

    def create_container(self, **kwargs: Any) -> None:
        self.calls.append(("create_container", kwargs))
        if self.exists:
            raise ResourceExistsError("ContainerAlreadyExists")
        self.exists = True

    def get_blob_client(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)

    def upload_blob(self, name: str, data: bytes, **kwargs: Any) -> None:
        FakeBlob(self, name).upload_blob(data, **kwargs)


class FakeService:
    def __init__(self) -> None:
        self.containers: dict[str, FakeContainer] = {}

    def get_container_client(self, name: str) -> FakeContainer:
        return self.containers.setdefault(name, FakeContainer(name))


@pytest.fixture
def blob_settings(settings_env, monkeypatch):
    monkeypatch.setenv("ZZ2_ENABLE_BLOB_STORAGE", "true")
    monkeypatch.setenv("ZZ2_AZURE_STORAGE_CONNECTION_STRING", CONNECTION_STRING)
    monkeypatch.setenv("ZZ2_AZURE_STORAGE_CONTAINER", "zz2-manuals")
    from zz2.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def service(blob_settings) -> FakeService:
    return FakeService()


@pytest.fixture
def gateway(service) -> AzureBlobStorageGateway:
    return AzureBlobStorageGateway(service_client=service)


def container(service: FakeService) -> FakeContainer:
    return service.containers["zz2-manuals"]


# -- configuration ----------------------------------------------------------------


def test_disabled_by_default(settings_env, monkeypatch) -> None:
    monkeypatch.delenv("ZZ2_ENABLE_BLOB_STORAGE", raising=False)
    monkeypatch.delenv("ZZ2_AZURE_STORAGE_CONNECTION_STRING", raising=False)
    from zz2.settings import get_settings

    get_settings.cache_clear()
    gateway = AzureBlobStorageGateway(service_client=FakeService())
    assert gateway.configured is False
    with pytest.raises(NotConfigured):
        gateway.download_manual("ITZ-X", "manual.pdf")
    get_settings.cache_clear()


def test_invalid_container_name_is_not_configured(blob_settings, monkeypatch) -> None:
    monkeypatch.setenv("ZZ2_AZURE_STORAGE_CONTAINER", "Bad_Name")
    from zz2.settings import get_settings

    get_settings.cache_clear()
    with pytest.raises(NotConfigured):
        AzureBlobStorageGateway(service_client=FakeService()).upload_manual(
            "ITZ-X", "m.pdf", b"%PDF"
        )


def test_sdk_client_is_built_lazily_from_the_connection_string(blob_settings) -> None:
    gateway = AzureBlobStorageGateway()
    assert gateway._client is None
    assert gateway.configured is True

    blob = gateway._blob("ITZ X", "Install guide.pdf")

    assert gateway._client is not None
    assert blob.container_name == "zz2-manuals"
    assert blob.blob_name == "ITZ X/Install guide.pdf"
    assert blob.account_name == "zz2test"
    # Reused, not rebuilt, while the connection string is unchanged.
    first = gateway._client
    gateway._blob("ITZ-X", "other.pdf")
    assert gateway._client is first


def test_malformed_connection_string_is_not_echoed(blob_settings, monkeypatch) -> None:
    monkeypatch.setenv("ZZ2_AZURE_STORAGE_CONNECTION_STRING", "AccountKey=topsecret-value")
    from zz2.settings import get_settings

    get_settings.cache_clear()
    with pytest.raises(NotConfigured) as caught:
        AzureBlobStorageGateway()._blob("ITZ-X", "manual.pdf")
    assert "topsecret" not in caught.value.message
    assert "topsecret" not in (caught.value.log_detail or "")


# -- container ----------------------------------------------------------------------


def test_container_is_created_private_once(gateway, service) -> None:
    gateway.ensure_bucket()
    gateway.ensure_container()

    calls = container(service).calls
    assert calls == [("create_container", {})]  # no public_access => private


def test_existing_container_is_reused(gateway, service) -> None:
    service.get_container_client("zz2-manuals").exists = True

    gateway.ensure_bucket()

    assert container(service).exists is True


def test_container_creation_failure_is_an_upstream_error(gateway, service) -> None:
    fake = service.get_container_client("zz2-manuals")

    def broken(**_kwargs: Any) -> None:
        raise ServiceRequestError("secret internal hostname")

    fake.create_container = broken
    with pytest.raises(UpstreamError) as caught:
        gateway.ensure_bucket()
    assert "secret internal hostname" not in caught.value.message


# -- writes ---------------------------------------------------------------------------


def test_upload_manual_overwrites_with_pdf_content_type(gateway, service) -> None:
    gateway.upload_manual("ITZ X", "Install guide.pdf", b"%PDF-1.7")
    gateway.upload_manual("ITZ X", "Install guide.pdf", b"%PDF-1.7 v2")

    data, settings = container(service).blobs["ITZ X/Install guide.pdf"]
    assert data == b"%PDF-1.7 v2"
    assert settings.content_type == "application/pdf"
    assert settings.cache_control == "max-age=300"
    assert ("upload", "ITZ X/Install guide.pdf", True) in container(service).calls


def test_image_upload_and_download_use_the_same_private_container(gateway, service) -> None:
    gateway.upload_image("IT2-BNT", "wiring.jpg", b"png bytes", content_type="image/png")
    downloaded = gateway.download_image("IT2-BNT", "wiring.jpg")

    _data, settings = container(service).blobs["IT2-BNT/wiring.jpg"]
    assert settings.content_type == "image/png"
    assert downloaded.status_code == 200
    assert downloaded.content == b"png bytes"
    assert downloaded.headers["Etag"] == '"0x8DDBLOB"'
    assert downloaded.headers["Last-Modified"] == "Sun, 01 Nov 2026 12:00:00 GMT"
    assert downloaded.headers["Content-Length"] == "9"


def test_upload_failure_is_an_upstream_error_without_details(gateway, service) -> None:
    gateway.ensure_bucket()
    container(service).fail_with = HttpResponseError("AuthenticationFailed for zz2test")

    with pytest.raises(UpstreamError) as caught:
        gateway.upload_manual("ITZ-X", "manual.pdf", b"%PDF")
    assert caught.value.message == "The file could not be stored. Try again shortly."
    assert "zz2test" not in (caught.value.log_detail or "")


def test_copy_manual_duplicates_the_object(gateway, service) -> None:
    gateway.upload_manual("SRC", "manual.pdf", b"%PDF source")

    gateway.copy_manual("SRC", "DST", "manual.pdf")

    data, settings = container(service).blobs["DST/manual.pdf"]
    assert data == b"%PDF source"
    assert settings.content_type == "application/pdf"
    assert "SRC/manual.pdf" in container(service).blobs


def test_copy_of_a_missing_manual_is_not_found(gateway) -> None:
    with pytest.raises(NotFound):
        gateway.copy_manual("SRC", "DST", "missing.pdf")


def test_delete_is_scoped_to_one_object(gateway, service) -> None:
    gateway.upload_manual("ITZ-X", "manual.pdf", b"%PDF")
    gateway.upload_manual("ITZ-X", "other.pdf", b"%PDF")

    gateway.delete_manual("ITZ-X", "manual.pdf")
    gateway.delete_manual("ITZ-X", "manual.pdf")  # already gone: not an error

    assert list(container(service).blobs) == ["ITZ-X/other.pdf"]


# -- reads ----------------------------------------------------------------------------


def test_download_honours_pdf_ranges(gateway, service) -> None:
    gateway.upload_manual("ITZ-X", "manual.pdf", b"0123456789" * 10)

    downloaded = gateway.download_manual("ITZ-X", "manual.pdf", range_header="bytes=0-8")

    assert downloaded.status_code == 206
    assert downloaded.content == b"012345678"
    assert downloaded.headers["Content-Range"] == "bytes 0-8/100"
    assert downloaded.headers["Content-Length"] == "9"
    assert ("download", "ITZ-X/manual.pdf", 0, 9) in container(service).calls


def test_open_ended_and_suffix_ranges(gateway) -> None:
    gateway.upload_manual("ITZ-X", "manual.pdf", b"0123456789")

    tail = gateway.download_manual("ITZ-X", "manual.pdf", range_header="bytes=7-")
    suffix = gateway.download_manual("ITZ-X", "manual.pdf", range_header="bytes=-3")
    clipped = gateway.download_manual("ITZ-X", "manual.pdf", range_header="bytes=8-50")

    assert (tail.status_code, tail.content) == (206, b"789")
    assert tail.headers["Content-Range"] == "bytes 7-9/10"
    assert (suffix.status_code, suffix.content) == (206, b"789")
    assert suffix.headers["Content-Range"] == "bytes 7-9/10"
    assert clipped.headers["Content-Range"] == "bytes 8-9/10"


def test_unsatisfiable_range_is_a_416(gateway) -> None:
    gateway.upload_manual("ITZ-X", "manual.pdf", b"0123456789")

    downloaded = gateway.download_manual("ITZ-X", "manual.pdf", range_header="bytes=50-60")

    assert downloaded.status_code == 416
    assert downloaded.headers["Content-Range"] == "bytes */10"


def test_malformed_range_serves_the_whole_object(gateway) -> None:
    gateway.upload_manual("ITZ-X", "manual.pdf", b"0123456789")

    downloaded = gateway.download_manual("ITZ-X", "manual.pdf", range_header="bytes=5-2")

    assert downloaded.status_code == 200
    assert downloaded.content == b"0123456789"
    assert "Content-Range" not in downloaded.headers


def test_missing_storage_object_is_a_not_found(gateway) -> None:
    with pytest.raises(NotFound):
        gateway.download_manual("ITZ-X", "missing.pdf")
    with pytest.raises(NotFound):
        gateway.download_manual("ITZ-X", "missing.pdf", range_header="bytes=-5")


def test_storage_transport_failures_are_not_leaked(gateway, service) -> None:
    gateway.ensure_bucket()
    container(service).fail_with = ServiceRequestError("secret internal hostname")

    with pytest.raises(UpstreamError) as caught:
        gateway.download_manual("ITZ-X", "manual.pdf")
    assert "secret internal hostname" not in caught.value.message
    assert "secret internal hostname" not in (caught.value.log_detail or "")


# -- routes ---------------------------------------------------------------------------


def test_authenticated_image_route_falls_back_to_storage(app, monkeypatch) -> None:
    monkeypatch.setattr(
        type(storage_gateway), "configured", property(lambda _gateway: True)
    )
    monkeypatch.setattr(
        type(storage_gateway),
        "download_image",
        lambda _gateway, kit, filename: StorageDownload(
            content=b"\xff\xd8\xffremote diagram",
            status_code=200,
            headers={"Etag": "diagram-v1"},
        ),
    )

    response = app.get("/images/REMOTE-KIT/dip.jpg", headers=auth_header())

    assert response.status_code == 200
    assert response.content == b"\xff\xd8\xffremote diagram"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_authenticated_manual_route_streams_a_blob_range(app, monkeypatch) -> None:
    fake_service = FakeService()
    fake_gateway = AzureBlobStorageGateway(service_client=fake_service)
    monkeypatch.setenv("ZZ2_ENABLE_BLOB_STORAGE", "true")
    monkeypatch.setenv("ZZ2_AZURE_STORAGE_CONNECTION_STRING", CONNECTION_STRING)
    from zz2.settings import get_settings

    get_settings.cache_clear()
    fake_gateway.upload_manual("REMOTE-KIT", "guide.pdf", b"%PDF-1.7 remote body")
    monkeypatch.setattr("zz2.api.routers.pages.storage_gateway", fake_gateway)

    response = app.get(
        "/manuals/REMOTE-KIT/guide.pdf",
        headers={**auth_header(), "Range": "bytes=0-7"},
    )

    assert response.status_code == 206
    assert response.content == b"%PDF-1.7"
    assert response.headers["content-range"] == "bytes 0-7/20"
    assert response.headers["content-type"] == "application/pdf"
    get_settings.cache_clear()


def test_frontend_fetches_private_media_with_the_access_token() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "web/static/js/index.3.js"
    ).read_text(encoding="utf-8")

    assert "async function _loadKitImage" in source
    assert "async function _loadKitManual" in source
    assert "'Authorization': 'Bearer ' + token" in source
    direct_private_image = """<img id="' + id + 'I" src="' + src"""
    assert direct_private_image not in source


# -- Supabase -> Blob migration script ------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, content: bytes = b"",
                 headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = headers or {}
        self.text = ""

    def json(self) -> Any:
        return self._payload


class FakeSupabaseHttp:
    """Serves a bucket with folders (``id: null``) and paginated listings."""

    def __init__(self, tree: dict[str, list[dict[str, Any]]], bodies: dict[str, bytes]) -> None:
        self.tree = tree
        self.bodies = bodies
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("POST", url, kwargs))
        body = kwargs["json"]
        entries = self.tree.get(body["prefix"], [])
        page = entries[body["offset"]: body["offset"] + body["limit"]]
        return FakeResponse(200, payload=page)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        path = url.split("/storage/v1/object/zz2-manuals/", 1)[1]
        from urllib.parse import unquote

        path = unquote(path)
        if path not in self.bodies:
            return FakeResponse(404)
        return FakeResponse(200, content=self.bodies[path],
                            headers={"content-type": "application/pdf"})


def _file(name: str, size: int, mimetype: str) -> dict[str, Any]:
    return {"name": name, "id": f"id-{name}", "metadata": {"size": size, "mimetype": mimetype}}


def _supabase_fixture() -> FakeSupabaseHttp:
    tree = {
        "": [{"name": "ITZ X", "id": None, "metadata": None},
             {"name": "IT2-BNT", "id": None, "metadata": None}],
        "ITZ X": [_file("Install guide.pdf", 8, "application/pdf")],
        "IT2-BNT": [_file("wiring.jpg", 4, "image/jpeg"), _file("gone.pdf", 3, "")],
    }
    bodies = {"ITZ X/Install guide.pdf": b"%PDF-1.7", "IT2-BNT/wiring.jpg": b"jpeg"}
    return FakeSupabaseHttp(tree, bodies)


def test_migration_copies_every_object_and_preserves_content_type() -> None:
    http = _supabase_fixture()
    source = SupabaseStorageSource("https://p.supabase.co/", "service-key", "zz2-manuals",
                                   client=http)
    target = FakeContainer("zz2-manuals")
    out = io.StringIO()

    summary = migrate(source, target, out=out)

    assert summary.copied == 2
    assert summary.bytes == 12
    assert [path for path, _ in summary.failures] == ["IT2-BNT/gone.pdf"]
    assert target.blobs["ITZ X/Install guide.pdf"][0] == b"%PDF-1.7"
    assert target.blobs["ITZ X/Install guide.pdf"][1].content_type == "application/pdf"
    assert target.blobs["IT2-BNT/wiring.jpg"][1].content_type == "image/jpeg"
    list_call = http.calls[0]
    assert list_call[1] == "https://p.supabase.co/storage/v1/object/list/zz2-manuals"
    assert list_call[2]["headers"] == {"apikey": "service-key",
                                       "Authorization": "Bearer service-key"}
    assert list_call[2]["json"]["limit"] == 1000
    assert any(call[1].endswith("/object/zz2-manuals/ITZ%20X/Install%20guide.pdf")
               for call in http.calls if call[0] == "GET")
    assert "service-key" not in out.getvalue()


def test_migration_dry_run_writes_nothing() -> None:
    http = _supabase_fixture()
    source = SupabaseStorageSource("https://p.supabase.co", "k", "zz2-manuals", client=http)

    summary = migrate(source, None, dry_run=True, out=io.StringIO())

    assert summary.copied == 3
    assert summary.bytes == 15
    assert not [call for call in http.calls if call[0] == "GET"]


def test_migration_listing_paginates() -> None:
    entries = [_file(f"f{i}.pdf", 1, "application/pdf") for i in range(1001)]
    http = FakeSupabaseHttp({"": entries}, {})
    source = SupabaseStorageSource("https://p.supabase.co", "k", "zz2-manuals", client=http)

    paths = [obj.path for obj in source.iter_objects()]

    assert len(paths) == 1001
    assert [call[2]["json"]["offset"] for call in http.calls] == [0, 1000]


# -- folder-manual import script (unchanged behaviour) --------------------------------


def test_recursive_manual_discovery_matches_flexible_folder_names(tmp_path) -> None:
    exact = tmp_path / "Audi" / "IT3-AUDI" / "Install.pdf"
    flexible = tmp_path / "BMW" / "IT3 NBT" / "Manual.pdf"
    unknown = tmp_path / "Unknown Kit" / "Guide.pdf"
    for path in (exact, flexible, unknown):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.7")

    matched, unmatched = discover_manuals(tmp_path, ["IT3-AUDI", "IT3-NBT"])

    assert matched == {"IT3-AUDI": [exact], "IT3-NBT": [flexible]}
    assert unmatched == [unknown]


def test_primary_manual_prefers_exact_kit_filename_then_manual_name(tmp_path) -> None:
    generic = tmp_path / "01.pdf"
    named = tmp_path / "Installation Manual.pdf"
    exact = tmp_path / "IT3-AUDI.pdf"

    assert choose_primary("IT3-AUDI", [generic, named, exact]) == exact
    assert choose_primary("IT3-AUDI", [generic, named]) == named


def test_destination_name_is_safe_for_storage_and_download_route(tmp_path) -> None:
    manual = tmp_path / "Manual de instalação (Áudio).pdf"

    assert destination_name("IT3-AUDI", manual) == "Manual_de_instala__o___udio_.pdf"
