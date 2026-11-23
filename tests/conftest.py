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
# Never let a developer's real .env turn an isolated unit test into a Storage
# upload. Integration-specific tests opt in explicitly with monkeypatch.
os.environ["ZZ2_ENABLE_BLOB_STORAGE"] = "false"

import pytest  # noqa: E402

from zz2.settings import Settings  # noqa: E402

# Never read the developer's .env: its trusted hosts, rate limits and feature
# flags would otherwise leak into tests that assume the defaults.
Settings.model_config["env_file"] = None


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
        "ZZ2_SESSION_SECRET": "test-session-secret-value-at-least-32-chars",
        "ZZ2_APPSCRIPT_URL": "https://script.google.com/macros/s/test/exec",
        "ZZ2_APPSCRIPT_SECRET": "appscript-test-secret",
        "ZZ2_RATE_LIMIT_ENABLED": "false",
        "ZZ2_ENABLE_BACKGROUND_WORKERS": "false",
        "ZZ2_ENABLE_DB_MIRROR": "false",
        "ZZ2_ENABLE_BLOB_STORAGE": "false",
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


class FakeUserDirectory:
    """In-memory stand-in for :data:`zz2.gateways.user_directory.user_directory`."""

    PERMANENT_BAN = "infinity"

    def __init__(self) -> None:
        self.users: list[dict[str, Any]] = []
        self.passwords: dict[str, str] = {}
        self.configured = True

    def seed(self, *, email: str, role: str, permissions: list[str] | None = None,
             display: str = "", active: bool = True, password: str | None = None,
             microsoft_oid: str | None = None) -> str:
        import uuid

        user_id = str(uuid.uuid4())
        self.users.append({
            "id": user_id,
            "email": email.lower(),
            "app_metadata": {"role": role, "permissions": permissions or []},
            "user_metadata": {"display_name": display or email},
            "banned_until": None if active else "infinity",
            "last_sign_in_at": None,
            "microsoft_linked": bool(microsoft_oid),
        })
        if password:
            self.passwords[user_id] = password
        return user_id

    def list_users(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [dict(user) for user in self.users]

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        return next((dict(u) for u in self.users if u["id"] == user_id), None)

    def find_by_email(self, email: str) -> dict[str, Any] | None:
        return next((dict(u) for u in self.users if u["email"] == email.strip().lower()), None)

    def authenticate(self, email: str, password: str) -> dict[str, Any] | None:
        user = self.find_by_email(email)
        if not user or self.passwords.get(user["id"]) != password or user["banned_until"]:
            return None
        return user

    def touch_sign_in(self, user_id: str, *, microsoft_oid: str | None = None) -> None:
        for user in self.users:
            if user["id"] == user_id:
                user["last_sign_in_at"] = "now"
                if microsoft_oid:
                    user["microsoft_linked"] = True

    def create_user(self, *, email: str, password: str | None, display: str,
                    role: str, permissions: list[str],
                    microsoft_oid: str | None = None) -> dict[str, Any]:
        from zz2.core.errors import UpstreamError

        if self.find_by_email(email):
            raise UpstreamError("An account with that email address already exists.")
        user_id = self.seed(email=email, role=role, permissions=permissions,
                            display=display, password=password, microsoft_oid=microsoft_oid)
        return self.get_user(user_id) or {}

    def update_user(self, user_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        for user in self.users:
            if user["id"] == user_id:
                if "app_metadata" in changes:
                    user["app_metadata"] = changes["app_metadata"]
                if "user_metadata" in changes:
                    user["user_metadata"] = changes["user_metadata"]
                if "ban_duration" in changes:
                    user["banned_until"] = (None if changes["ban_duration"] in (None, "", "none")
                                            else "infinity")
                if changes.get("password"):
                    self.passwords[user_id] = changes["password"]
                return dict(user)
        from zz2.core.errors import NotFound

        raise NotFound("User not found.")

    def set_password(self, user_id: str, password: str) -> dict[str, Any]:
        return self.update_user(user_id, {"password": password})

    def delete_user(self, user_id: str) -> None:
        self.users = [user for user in self.users if user["id"] != user_id]


FakeSupabaseAdmin = FakeUserDirectory  # old name, kept for existing type hints


class FakeDatabase:
    """Stand-in for :data:`zz2.gateways.database.db`.

    Records every statement as ``(sql, params)`` and replays scripted answers.
    :meth:`stub` keys on a fragment of the SQL text (normalised by :meth:`_text`), so a
    test describes only the query it cares about. A stubbed answer may be rows, a
    callable ``(sql, params) -> rows``, or an exception instance to raise.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.responses: list[tuple[str, Any]] = []
        self.configured = True

    @staticmethod
    def _text(query: Any) -> str:
        """SQL text with whitespace collapsed and identifier quotes dropped, so a
        test can match ``INSERT INTO t (a, b)`` however the statement was built."""
        text = query if isinstance(query, str) else query.as_string(None)
        return " ".join(text.replace('"', "").split())

    def stub(self, fragment: str, response: Any) -> None:
        # Newest first, so a test can override a fixture's broader stub.
        self.responses.insert(0, (" ".join(fragment.split()), response))

    def statements(self, fragment: str = "") -> list[tuple[str, Any]]:
        """Recorded ``(sql, params)`` whose SQL contains ``fragment``."""
        return [call for call in self.calls if fragment in call[0]]

    def _answer(self, query: Any, params: Any) -> Any:
        text = self._text(query)
        self.calls.append((text, params))
        for fragment, response in self.responses:
            if fragment in text:
                if isinstance(response, BaseException):
                    raise response
                return response(text, params) if callable(response) else response
        return None

    def fetch_all(self, query: Any, params: Any = None) -> list[dict[str, Any]]:
        result = self._answer(query, params)
        return [dict(row) for row in result] if isinstance(result, list) else []

    def fetch_one(self, query: Any, params: Any = None) -> dict[str, Any] | None:
        result = self._answer(query, params)
        if isinstance(result, list):
            return dict(result[0]) if result else None
        return dict(result) if isinstance(result, dict) else None

    def execute(self, query: Any, params: Any = None) -> int:
        result = self._answer(query, params)
        return result if isinstance(result, int) else 1

    def execute_many(self, query: Any, rows: Any) -> None:
        for params in rows:
            self._answer(query, params)

    def upsert(self, table: str, rows: list[dict[str, Any]], *,
               conflict: Any) -> None:
        self.calls.append((f"UPSERT {table}", rows))

    def ping(self) -> bool:
        return True


def _patch_singleton(monkeypatch: pytest.MonkeyPatch, name: str, value: Any,
                     modules: tuple[str, ...]) -> None:
    """Replace a gateway singleton in every namespace that imported it.

    Each module imported the object by value, so it has to be swapped in each
    one. Listing the modules explicitly is deliberate: a new module that talks to
    a gateway shows up here as a failing test rather than as real network traffic
    during the suite.
    """
    for module_path in modules:
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError:  # e.g. a gateway removed by the Azure migration
            continue
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)


@pytest.fixture
def fake_supabase(monkeypatch: pytest.MonkeyPatch) -> FakeSupabase:
    fake = FakeSupabase()
    _patch_singleton(monkeypatch, "supabase", fake, (
        "zz2.gateways.supabase",
        "zz2.services.auth",
        "zz2.services.tickets",
        "zz2.services.customers",
        "zz2.services.mirror",
        "zz2.repositories.troubleshooting_sessions",
    ))
    return fake


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    fake = FakeDatabase()
    _patch_singleton(monkeypatch, "db", fake, (
        "zz2.services.tickets",
        "zz2.services.customers",
        "zz2.services.mirror",
        "zz2.repositories.troubleshooting_sessions",
        "zz2.api.routers.meta",
        "zz2.api.routers.ticket_records",
    ))
    return fake


@pytest.fixture
def fake_admin(monkeypatch: pytest.MonkeyPatch) -> FakeSupabaseAdmin:
    fake = FakeSupabaseAdmin()
    _patch_singleton(monkeypatch, "user_directory", fake, (
        "zz2.gateways.user_directory",
        "zz2.services.auth",
        "zz2.services.users",
        "zz2.services.roles",
        "zz2.services.sso",
        "zz2.api.routers.auth",
    ))
    return fake


@pytest.fixture
def app(
    settings_env: None,
    data_dir: Path,
    fake_supabase: FakeSupabase,
    fake_admin: FakeSupabaseAdmin,
    fake_db: FakeDatabase,
) -> Iterator[Any]:
    """A configured application with lifespan startup run."""
    from fastapi.testclient import TestClient

    from zz2.main import create_app
    from zz2.services import session_tokens
    from zz2.services.auth import _principal_cache
    from zz2.services.lockout import lockout_guard
    from zz2.services.saml import saml_sp
    from zz2.services.sso import sso_service

    # In-memory security state is process-wide; one test's lockout or pending
    # SAML request must not leak into the next.
    for resettable in (lockout_guard, saml_sp, sso_service, session_tokens):
        resettable.reset()

    # The principal cache is process-wide and keyed on the token. Tokens are
    # regenerated per test, but clearing keeps a role change in one test from
    # being served from cache in the next.
    _principal_cache.clear()

    from zz2.services.presence import presence_hub

    presence_hub.reset()

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
    provider: str = "password",
) -> str:
    """Mint a session token exactly as the server does after a sign-in.

    Signed with the secret ``settings_env`` configures, so the real verification
    path runs — issuer, audience, expiry and revocation included.
    """
    import time
    import uuid

    from jose import jwt

    now = int(time.time())
    claims = {
        "iss": "zz2",
        "aud": "zz2",
        "sub": subject,
        "email": email,
        "iat": now,
        "nbf": now - 5,
        "exp": now + expires_in,
        # A nonce, so two tokens minted in the same second with different roles
        # are distinct cache keys.
        "jti": uuid.uuid4().hex,
        "amr": [provider],
        "app_metadata": {"role": role, "permissions": permissions or [], "provider": provider},
        "user_metadata": {"display_name": display},
    }
    return jwt.encode(claims, os.environ["ZZ2_SESSION_SECRET"], algorithm="HS256")


def auth_header(**kwargs: Any) -> dict[str, str]:
    """``Authorization`` header for a freshly minted token."""
    return {"Authorization": f"Bearer {make_token(**kwargs)}"}
