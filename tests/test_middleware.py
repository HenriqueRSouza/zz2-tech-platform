"""Middleware behaviour that the other suites disable.

``settings_env`` turns rate limiting off so the rest of the tests are not fighting
a budget. These build their own app with it on, because a rate limiter that is
never exercised is a rate limiter that might not work.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tests.conftest import auth_header


@pytest.fixture
def limited_app(monkeypatch: pytest.MonkeyPatch, data_dir, fake_supabase,
                fake_admin, fake_db) -> Iterator[Any]:
    """An app with a deliberately tiny rate-limit budget."""
    from fastapi.testclient import TestClient

    for key, value in {
        "ZZ2_ENVIRONMENT": "development",
        "ZZ2_BUILD": "test",
        "ZZ2_SESSION_SECRET": "test-session-secret-value-at-least-32-chars",
        "ZZ2_ENABLE_BACKGROUND_WORKERS": "false",
        "ZZ2_ENABLE_DB_MIRROR": "false",
        "ZZ2_LOG_JSON": "false",
        "ZZ2_RATE_LIMIT_ENABLED": "true",
        "ZZ2_RATE_LIMIT_REQUESTS": "5",
        "ZZ2_RATE_LIMIT_WRITE_REQUESTS": "2",
        "ZZ2_RATE_LIMIT_WINDOW_SECONDS": "60",
    }.items():
        monkeypatch.setenv(key, value)

    from zz2.main import create_app
    from zz2.services.auth import _principal_cache
    from zz2.settings import get_settings

    get_settings.cache_clear()
    _principal_cache.clear()
    try:
        with TestClient(create_app()) as client:
            yield client
    finally:
        get_settings.cache_clear()


class TestRateLimiting:
    def test_reads_are_capped(self, limited_app) -> None:
        statuses = [limited_app.get("/api/config").status_code for _ in range(8)]
        assert statuses[:5] == [200] * 5
        assert 429 in statuses[5:]

    def test_a_blocked_response_says_when_to_retry(self, limited_app) -> None:
        for _ in range(6):
            response = limited_app.get("/api/config")
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) > 0
        assert response.json()["code"] == "rate_limited"

    def test_writes_have_their_own_tighter_budget(self, limited_app) -> None:
        """A dashboard polling across eight tabs is legitimate and read-heavy;
        the same volume of writes is not."""
        header = auth_header(permissions=["create_tickets"])
        statuses = [
            limited_app.post("/api/tickets", json={"client": f"C{i}"},
                             headers=header).status_code
            for i in range(4)
        ]
        assert 429 in statuses, "the write budget was not enforced"
        # The read budget is larger, so reads still work after the writes blocked.
        assert limited_app.get("/api/config").status_code == 200

    def test_health_probes_are_never_limited(self, limited_app) -> None:
        """A throttled health check reports the service down under load."""
        for _ in range(30):
            assert limited_app.get("/healthz").status_code == 200

    def test_distinct_tokens_get_distinct_budgets(self, limited_app) -> None:
        """Keyed on identity before IP, so one office behind a NAT does not
        share a single budget."""
        first = auth_header(subject="u-1", email="one@zz2.com.br")
        second = auth_header(subject="u-2", email="two@zz2.com.br")

        for _ in range(6):
            limited_app.post("/api/session/verify", headers=first)

        assert limited_app.post("/api/session/verify",
                                headers=second).status_code == 200


class TestAuditTrail:
    """Denials are the highest-signal security event a web app produces. The
    monolith logged none of them."""

    def _audit_lines(self) -> list[str]:
        from zz2.paths import AUDIT_LOG

        if not AUDIT_LOG.exists():
            return []
        return AUDIT_LOG.read_text(encoding="utf-8", errors="replace").splitlines()

    def test_a_permission_denial_is_audited(self, app) -> None:
        before = len(self._audit_lines())
        response = app.get("/api/system/info",
                           headers=auth_header(role="tech", permissions=[]))
        assert response.status_code == 403

        new_lines = self._audit_lines()[before:]
        assert any("authz.denied" in line for line in new_lines)
        assert any("system_settings" in line for line in new_lines)

    def test_a_privileged_action_is_audited_with_the_actor(self, app) -> None:
        before = len(self._audit_lines())
        app.post("/api/kit/new", json={"key": "ITZ-AUDIT"},
                 headers=auth_header(email="editor@zz2.com.br",
                                     permissions=["kit_editor"]))

        new_lines = self._audit_lines()[before:]
        assert any("kit.created" in line for line in new_lines)
        assert any("editor@zz2.com.br" in line for line in new_lines)

    def test_credentials_never_reach_the_audit_log(self, app) -> None:
        from tests.conftest import make_token

        token = make_token(permissions=["kit_editor"])
        app.post("/api/kit/new", json={"key": "ITZ-SCRUB"},
                 headers={"Authorization": f"Bearer {token}"})

        contents = "\n".join(self._audit_lines())
        assert token not in contents
        assert "sb_secret_test" not in contents


class TestRequestContext:
    def test_a_request_id_is_returned_and_echoed(self, app) -> None:
        supplied = "abc123def456"
        response = app.get("/api/config", headers={"X-Request-ID": supplied})
        assert response.headers["X-Request-ID"] == supplied

    def test_a_request_id_is_generated_when_absent(self, app) -> None:
        assert len(app.get("/api/config").headers["X-Request-ID"]) >= 8

    def test_an_unhandled_error_returns_the_request_id(
        self, settings_env, data_dir, fake_supabase, fake_admin, monkeypatch
    ) -> None:
        """It is the only thing the client needs in order to let someone find the
        real cause in the log.

        Built with ``raise_server_exceptions=False``: the TestClient's default is
        to re-raise so a developer sees the traceback, which would bypass the very
        handler under test. Production returns the response.
        """
        from fastapi.testclient import TestClient

        from zz2.main import create_app
        from zz2.services.runtime_config import RuntimeConfigService

        def explode(self: RuntimeConfigService) -> int:
            raise RuntimeError("simulated bug")

        # Patched on the class: pane_refresh_interval is a property, and a
        # property cannot be replaced on an instance.
        monkeypatch.setattr(RuntimeConfigService, "pane_refresh_interval",
                            property(explode))

        with TestClient(create_app(), raise_server_exceptions=False) as client:
            response = client.get("/api/config")

        assert response.status_code == 500
        body = response.json()
        assert body["code"] == "internal_error"
        assert body["request_id"]
        assert "simulated bug" not in response.text
        assert "Traceback" not in response.text


class TestThirdPartyLogRecords:
    """Records from uvicorn and other libraries pass through our handlers.

    Both of these were live bugs: the text formatter raised ``KeyError:
    'request_id'`` on every uvicorn record, and — more importantly — credentials
    in a third-party record were not redacted, because the filters were attached
    to our loggers rather than to the handlers.
    """

    def test_a_record_without_request_context_renders(self, settings_env,
                                                      data_dir) -> None:
        import logging

        from zz2.core.logging import configure_logging, logger

        configure_logging(level="INFO", json_output=False)

        # Exactly how uvicorn's records arrive: a different logger, wired to our
        # handlers, with none of our context attributes set.
        third_party = logging.getLogger("uvicorn.error")
        third_party.handlers = logger.handlers
        third_party.setLevel(logging.INFO)

        third_party.info("Uvicorn running on http://127.0.0.1:8080")

        from zz2.paths import SERVER_LOG

        contents = SERVER_LOG.read_text(encoding="utf-8")
        assert "Uvicorn running" in contents
        assert "KeyError" not in contents

    def test_credentials_in_a_third_party_record_are_redacted(self, settings_env,
                                                              data_dir) -> None:
        import logging

        from zz2.core.logging import configure_logging, logger
        from zz2.paths import SERVER_LOG

        configure_logging(level="INFO", json_output=True)

        third_party = logging.getLogger("uvicorn.error")
        third_party.handlers = logger.handlers
        third_party.setLevel(logging.INFO)

        third_party.info("GET /api/proxy?action=recent&key=live-secret-value")

        contents = SERVER_LOG.read_text(encoding="utf-8")
        assert "live-secret-value" not in contents
        assert "[REDACTED]" in contents

    def test_both_formatters_survive_a_bare_record(self, settings_env,
                                                   data_dir) -> None:
        """A formatter that raises breaks logging itself — the worst place for a
        bug, because the failure hides every other failure."""
        import logging

        from zz2.core.logging import JsonFormatter, TextFormatter

        bare = logging.LogRecord("other.lib", logging.INFO, "f.py", 1,
                                 "plain message", (), None)
        assert "plain message" in JsonFormatter().format(bare)
        assert "plain message" in TextFormatter().format(bare)
