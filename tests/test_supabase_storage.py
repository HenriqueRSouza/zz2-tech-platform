"""Private Supabase Storage integration for kit manuals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from scripts.migrate_manuals_to_storage import (
    choose_primary,
    destination_name,
    discover_manuals,
)
from tests.conftest import auth_header
from zz2.core.errors import NotFound
from zz2.gateways.storage import (
    StorageDownload,
    SupabaseStorageGateway,
    storage_gateway,
)


@dataclass
class FakeResponse:
    status_code: int
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""


class FakeClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _next(self, method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("GET", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("POST", url, kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        return self._next(method, url, kwargs)


@pytest.fixture
def storage_settings(settings_env, monkeypatch):
    monkeypatch.setenv("ZZ2_ENABLE_SUPABASE_STORAGE", "true")
    monkeypatch.setenv("ZZ2_SUPABASE_STORAGE_BUCKET", "zz2-manuals")
    from zz2.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_bucket_is_created_private_for_pdf_and_images(storage_settings, monkeypatch) -> None:
    fake = FakeClient([FakeResponse(404), FakeResponse(200)])
    monkeypatch.setattr("zz2.gateways.storage.get_client", lambda: fake)

    gateway = SupabaseStorageGateway()
    gateway.ensure_bucket()
    gateway.ensure_bucket()

    assert [call[0] for call in fake.calls] == ["GET", "POST"]
    payload = fake.calls[1][2]["json"]
    assert payload["public"] is False
    assert payload["allowed_mime_types"] == [
        "application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp"
    ]


def test_upload_uses_a_server_authorized_upsert(storage_settings, monkeypatch) -> None:
    fake = FakeClient([FakeResponse(200), FakeResponse(200)])
    monkeypatch.setattr("zz2.gateways.storage.get_client", lambda: fake)

    gateway = SupabaseStorageGateway()
    gateway.upload_manual("ITZ X", "Install guide.pdf", b"%PDF-1.7")

    method, url, kwargs = fake.calls[-1]
    assert method == "POST"
    assert url.endswith("/object/zz2-manuals/ITZ%20X/Install%20guide.pdf")
    assert kwargs["headers"]["x-upsert"] == "true"
    assert kwargs["headers"]["Content-Type"] == "application/pdf"
    assert kwargs["content"] == b"%PDF-1.7"


def test_download_forwards_pdf_ranges(storage_settings, monkeypatch) -> None:
    fake = FakeClient([
        FakeResponse(
            206,
            content=b"PDF slice",
            headers={"content-range": "bytes 0-8/100", "content-length": "9"},
        )
    ])
    monkeypatch.setattr("zz2.gateways.storage.get_client", lambda: fake)

    downloaded = SupabaseStorageGateway().download_manual(
        "ITZ-X", "manual.pdf", range_header="bytes=0-8"
    )

    assert downloaded.status_code == 206
    assert downloaded.content == b"PDF slice"
    assert downloaded.headers["Content-Range"] == "bytes 0-8/100"
    assert fake.calls[0][2]["headers"]["Range"] == "bytes=0-8"


def test_image_upload_and_download_use_the_same_private_bucket(
    storage_settings, monkeypatch
) -> None:
    fake = FakeClient([
        FakeResponse(200), FakeResponse(200),
        FakeResponse(200, content=b"jpeg bytes", headers={"etag": "diagram-v1"}),
    ])
    monkeypatch.setattr("zz2.gateways.storage.get_client", lambda: fake)

    gateway = SupabaseStorageGateway()
    gateway.upload_image("IT2-BNT", "wiring.jpg", b"jpeg bytes")
    downloaded = gateway.download_image("IT2-BNT", "wiring.jpg")

    assert fake.calls[1][1].endswith("/object/zz2-manuals/IT2-BNT/wiring.jpg")
    assert fake.calls[1][2]["headers"]["Content-Type"] == "image/jpeg"
    assert fake.calls[2][1].endswith(
        "/object/authenticated/zz2-manuals/IT2-BNT/wiring.jpg"
    )
    assert downloaded.content == b"jpeg bytes"


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
            headers={"ETag": "diagram-v1"},
        ),
    )

    response = app.get("/images/REMOTE-KIT/dip.jpg", headers=auth_header())

    assert response.status_code == 200
    assert response.content == b"\xff\xd8\xffremote diagram"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_missing_storage_object_is_a_not_found(storage_settings, monkeypatch) -> None:
    fake = FakeClient([FakeResponse(404)])
    monkeypatch.setattr("zz2.gateways.storage.get_client", lambda: fake)

    with pytest.raises(NotFound):
        SupabaseStorageGateway().download_manual("ITZ-X", "missing.pdf")


def test_delete_is_scoped_to_one_object(storage_settings, monkeypatch) -> None:
    fake = FakeClient([FakeResponse(200)])
    monkeypatch.setattr("zz2.gateways.storage.get_client", lambda: fake)

    SupabaseStorageGateway().delete_manual("ITZ-X", "manual.pdf")

    method, url, kwargs = fake.calls[0]
    assert method == "DELETE"
    assert url.endswith("/object/zz2-manuals")
    assert kwargs["json"] == {"prefixes": ["ITZ-X/manual.pdf"]}


def test_storage_transport_failures_are_not_leaked(storage_settings, monkeypatch) -> None:
    class BrokenClient:
        def get(self, *_args: Any, **_kwargs: Any) -> None:
            raise httpx.ConnectError("secret internal hostname")

    monkeypatch.setattr("zz2.gateways.storage.get_client", lambda: BrokenClient())

    from zz2.core.errors import UpstreamError

    with pytest.raises(UpstreamError) as caught:
        SupabaseStorageGateway().download_manual("ITZ-X", "manual.pdf")
    assert "secret internal hostname" not in caught.value.message


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
