"""Shared test fixtures.

Two properties every test depends on:

**Isolation.** ``ZZ2_DATA_DIR`` is set *at import time*, before any ``zz2`` module
is loaded, because :mod:`zz2.paths` resolves its constants once at import. Every
module that holds a path holds one under this temporary root. The :func:`data_dir`
fixture then wipes and recreates the tree between tests, which gives per-test
isolation without reloading modules — and it exercises the same
constants-resolved-at-import behaviour the production process has.

**No network.** Nothing here reaches Supabase or Apps Script. Gateways are
replaced with fakes, so a failure always means the code is wrong rather than that
a third party was slow.
"""

from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# Must happen before the first `import zz2.*` anywhere in the suite.
_TEST_DATA_ROOT = Path(tempfile.mkdtemp(prefix="zz2-tests-"))
os.environ["ZZ2_DATA_DIR"] = str(_TEST_DATA_ROOT)
os.environ.setdefault("ZZ2_ENVIRONMENT", "development")

import pytest  # noqa: E402


@pytest.fixture
def data_dir() -> Iterator[Path]:
    """An empty data directory, recreated for each test."""
    from zz2.paths import DATA_DIR, ensure_runtime_dirs

    for child in DATA_DIR.iterdir() if DATA_DIR.exists() else ():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    ensure_runtime_dirs()
    yield DATA_DIR


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Minimal development settings, with the singleton cache cleared."""
    for key, value in {
        "ZZ2_ENVIRONMENT": "development",
        "ZZ2_BUILD": "test",
        "ZZ2_SUPABASE_URL": "https://example.supabase.co",
        "ZZ2_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test",
        "ZZ2_SUPABASE_SERVICE_KEY": "sb_secret_test",
        "ZZ2_SUPABASE_JWT_SECRET": "test-jwt-secret-value-at-least-32-chars",
        "ZZ2_APPSCRIPT_URL": "https://script.google.com/macros/s/test/exec",
        "ZZ2_APPSCRIPT_SECRET": "appscript-test-secret",
        "ZZ2_RATE_LIMIT_ENABLED": "false",
        "ZZ2_ENABLE_BACKGROUND_WORKERS": "false",
        "ZZ2_ENABLE_SUPABASE_MIRROR": "false",
        "ZZ2_LOG_JSON": "false",
    }.items():
        monkeypatch.setenv(key, value)

    from zz2.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeSupabase:
    """Records calls and replays scripted responses.

    Keyed on ``(method, resource_prefix)`` so a test can stub one query without
    having to describe every other call the code under test happens to make.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.responses: dict[tuple[str, str], Any] = {}
        self.configured = True
        self.service_configured = True

    def stub(self, method: str, resource_prefix: str, response: Any) -> None:
        self.responses[(method.upper(), resource_prefix)] = response

    def request(self, method: str, resource: str, *, payload: Any = None,
                prefer: str = "", schema: str | None = None) -> Any:
        self.calls.append((method.upper(), resource, payload))
        for (stub_method, prefix), response in self.responses.items():
            if stub_method == method.upper() and resource.startswith(prefix):
                return response
        return None

    def select(self, resource: str, *, schema: str | None = None) -> list[dict[str, Any]]:
        result = self.request("GET", resource, schema=schema)
        return result if isinstance(result, list) else []

    def select_paged(self, template: str, *, page_size: int = 1000,
                     max_pages: int = 50, schema: str | None = None) -> list[dict[str, Any]]:
        return self.select(template.format(limit=page_size, offset=0), schema=schema)

    def upsert(self, table: str, rows: list[dict[str, Any]], *, on_conflict: str) -> None:
        self.calls.append(("UPSERT", table, rows))

    def delete_where(self, table: str, filter_query: str) -> None:
        self.calls.append(("DELETE", f"{table}?{filter_query}", None))

    def fetch_user(self, access_token: str) -> dict[str, Any]:
        """Stand in for /auth/v1/user.

        Present so the *remote* verification path is exercised rather than
        exploding with AttributeError. Decodes the test token without verifying
        it — verification is the real gateway's job, and the local path already
        covers signature and expiry.
        """
        from jose import jwt

        claims = jwt.get_unverified_claims(access_token)
        return {
            "id": claims.get("sub"),
            "email": claims.get("email"),
            "app_metadata": claims.get("app_metadata") or {},
            "user_metadata": claims.get("user_metadata") or {},
        }


class FakeSupabaseAdmin:
    """In-memory stand-in for the Supabase Auth Admin API."""

    PERMANENT_BAN = "876000h"

    def __init__(self) -> None:
        self.users: list[dict[str, Any]] = []
        self._next_id = 1

    def seed(self, *, email: str, role: str, permissions: list[str] | None = None,
             display: str = "", active: bool = True) -> str:
        user_id = f"uid-{self._next_id}"
        self._next_id += 1
        self.users.append({
            "id": user_id,
            "email": email,
            "app_metadata": {"role": role, "permissions": permissions or []},
            "user_metadata": {"display_name": display or email},
            "banned_until": None if active else "2126-01-01T00:00:00Z",
        })
        return user_id

    def list_users(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [dict(user) for user in self.users]

    def create_user(self, *, email: str, password: str, display: str,
                    role: str, permissions: list[str]) -> dict[str, Any]:
        from zz2.core.errors import UpstreamError

        if any(user["email"] == email for user in self.users):
            raise UpstreamError("An account with that email address already exists.")
        user_id = self.seed(email=email, role=role, permissions=permissions,
                            display=display)
        return {"id": user_id, "email": email}

    def update_user(self, user_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        for user in self.users:
            if user["id"] == user_id:
                if "app_metadata" in changes:
                    user["app_metadata"] = changes["app_metadata"]
                if "user_metadata" in changes:
                    user["user_metadata"] = changes["user_metadata"]
                if "ban_duration" in changes:
                    user["banned_until"] = (
                        None if changes["ban_duration"] == "none"
                        else "2126-01-01T00:00:00Z"
                    )
                return dict(user)
        from zz2.core.errors import NotFound

        raise NotFound("User not found.")

    def set_password(self, user_id: str, password: str) -> dict[str, Any]:
        return self.update_user(user_id, {})

    def delete_user(self, user_id: str) -> None:
        self.users = [user for user in self.users if user["id"] != user_id]


def _patch_singleton(monkeypatch: pytest.MonkeyPatch, name: str, value: Any,
                     modules: tuple[str, ...]) -> None:
    """Replace a gateway singleton in every namespace that imported it.

    Each module imported the object by value, so it has to be swapped in each
    one. Listing the modules explicitly is deliberate: a new module that talks to
    a gateway shows up here as a failing test rather than as real network traffic
    during the suite.
    """
    for module_path in modules:
        module = importlib.import_module(module_path)
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)


@pytest.fixture
def fake_supabase(monkeypatch: pytest.MonkeyPatch) -> FakeSupabase:
    fake = FakeSupabase()
    _patch_singleton(monkeypatch, "supabase", fake, (
        "zz2.gateways.supabase",
        "zz2.services.tickets",
        "zz2.services.customers",
        "zz2.services.mirror",
    ))
    return fake


@pytest.fixture
def fake_admin(monkeypatch: pytest.MonkeyPatch) -> FakeSupabaseAdmin:
    fake = FakeSupabaseAdmin()
    _patch_singleton(monkeypatch, "supabase_admin", fake, (
        "zz2.gateways.supabase",
        "zz2.services.users",
        "zz2.services.roles",
    ))
    return fake


@pytest.fixture
def app(
    settings_env: None,
    data_dir: Path,
    fake_supabase: FakeSupabase,
    fake_admin: FakeSupabaseAdmin,
) -> Iterator[Any]:
    """A configured application with lifespan startup run."""
    from fastapi.testclient import TestClient

    from zz2.main import create_app
    from zz2.services.auth import _principal_cache

    # The principal cache is process-wide and keyed on the token. Tokens are
    # regenerated per test, but clearing keeps a role change in one test from
    # being served from cache in the next.
    _principal_cache.clear()

    application = create_app()
    with TestClient(application) as client:
        yield client


def make_token(
    *,
    subject: str = "user-1",
    email: str = "tech@zz2.com.br",
    role: str = "tech",
    permissions: list[str] | None = None,
    display: str = "Test Tech",
    expires_in: int = 3600,
) -> str:
    """Mint a JWT the application will accept.

    Signed with the secret ``settings_env`` configures, so the real local
    verification path runs — audience and expiry checks included — rather than
    being stubbed out.
    """
    import time
    import uuid

    from jose import jwt

    claims = {
        "sub": subject,
        "email": email,
        "aud": "authenticated",
        "exp": int(time.time()) + expires_in,
        # A nonce, so two tokens minted in the same second with different roles
        # are distinct cache keys.
        "jti": uuid.uuid4().hex,
        "app_metadata": {"role": role, "permissions": permissions or []},
        "user_metadata": {"display_name": display},
    }
    return jwt.encode(claims, os.environ["ZZ2_SUPABASE_JWT_SECRET"], algorithm="HS256")


def auth_header(**kwargs: Any) -> dict[str, str]:
    """``Authorization`` header for a freshly minted token."""
    return {"Authorization": f"Bearer {make_token(**kwargs)}"}
