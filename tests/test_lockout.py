"""Brute-force guard: addresses presenting many distinct bad credentials are blocked."""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import auth_header

THRESHOLD = 5


@pytest.fixture
def guarded(app, monkeypatch):
    from zz2.settings import get_settings

    monkeypatch.setenv("ZZ2_AUTH_LOCKOUT_THRESHOLD", str(THRESHOLD))
    get_settings.cache_clear()
    return app


def bad_token() -> dict[str, str]:
    return {"Authorization": f"Bearer not-a-real-token-{uuid.uuid4().hex}"}


def test_distinct_bad_tokens_lock_the_address_out(guarded):
    for _ in range(THRESHOLD):
        assert guarded.post("/api/session/verify", headers=bad_token()).status_code == 401

    # Even a perfectly valid session is refused from a blocked address: the
    # attacker may hold one, and the block is about the address.
    response = guarded.post("/api/session/verify", headers=auth_header())
    assert response.status_code == 429
    assert response.json()["code"] == "locked_out"
    assert int(response.headers["Retry-After"]) > 0


def test_one_bad_token_retried_many_times_counts_once(guarded):
    header = bad_token()
    for _ in range(THRESHOLD * 4):
        assert guarded.post("/api/session/verify", headers=header).status_code == 401
    assert guarded.post("/api/session/verify", headers=auth_header()).status_code == 200


def test_expired_sessions_are_not_counted(guarded):
    for _ in range(THRESHOLD * 2):
        response = guarded.post("/api/session/verify",
                                headers=auth_header(expires_in=-60))
        assert response.status_code == 401
    assert guarded.post("/api/session/verify", headers=auth_header()).status_code == 200


def test_signed_out_requests_are_not_counted(guarded):
    for _ in range(THRESHOLD * 2):
        assert guarded.post("/api/session/verify").status_code == 401
    assert guarded.post("/api/session/verify", headers=auth_header()).status_code == 200


def test_health_checks_are_never_blocked(guarded):
    for _ in range(THRESHOLD):
        guarded.post("/api/session/verify", headers=bad_token())
    assert guarded.get("/healthz").status_code == 200


def test_administrator_can_list_and_release_a_block(guarded):
    from zz2.services.lockout import lockout_guard

    for _ in range(THRESHOLD):
        lockout_guard.record_failure("203.0.113.9", uuid.uuid4().hex, source="test")

    admin = auth_header(role="super_admin")
    listed = guarded.get("/api/system/security/lockouts", headers=admin).json()
    assert [row["address"] for row in listed["lockouts"]] == ["203.0.113.9"]

    released = guarded.post("/api/system/security/lockouts/release",
                            json={"address": "203.0.113.9"}, headers=admin)
    assert released.status_code == 200
    assert lockout_guard.retry_after("203.0.113.9") is None


def test_lockout_admin_requires_system_settings(guarded):
    response = guarded.get("/api/system/security/lockouts", headers=auth_header(role="tech"))
    assert response.status_code == 403


def test_disabled_guard_counts_nothing(guarded, monkeypatch):
    from zz2.settings import get_settings

    monkeypatch.setenv("ZZ2_AUTH_LOCKOUT_ENABLED", "false")
    get_settings.cache_clear()
    for _ in range(THRESHOLD * 2):
        guarded.post("/api/session/verify", headers=bad_token())
    assert guarded.post("/api/session/verify", headers=auth_header()).status_code == 200
