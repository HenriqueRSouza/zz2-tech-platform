"""Private Supabase Storage integration for kit manuals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from zz2.core.errors import NotFound
from zz2.gateways.storage import SupabaseStorageGateway


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


def test_bucket_is_created_private_and_pdf_only(storage_settings, monkeypatch) -> None:
    fake = FakeClient([FakeResponse(404), FakeResponse(200)])
    monkeypatch.setattr("zz2.gateways.storage.get_client", lambda: fake)

    gateway = SupabaseStorageGateway()
    gateway.ensure_bucket()
    gateway.ensure_bucket()

    assert [call[0] for call in fake.calls] == ["GET", "POST"]
    payload = fake.calls[1][2]["json"]
    assert payload["public"] is False
    assert payload["allowed_mime_types"] == ["application/pdf"]


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
