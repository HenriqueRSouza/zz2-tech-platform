"""Executed troubleshooting sessions: API, rules, statistics and both stores."""

from __future__ import annotations

import pytest

from tests.conftest import auth_header

KIT = "ITZ-NTG3"
TECH = {"email": "ana@zz2.com.br", "display": "Ana", "permissions": []}
OTHER_TECH = {"email": "bruno@zz2.com.br", "display": "Bruno"}
EDITOR = {"email": "editor@zz2.com.br", "role": "admin"}

PATH_A = ["No audio", "Check DIP switches", "DIP 3 ON"]
PATH_B = ["No audio", "Check wiring", "Replace harness"]


@pytest.fixture
def local_app(app, monkeypatch):
    from zz2.repositories.kits import KitRepository
    from zz2.settings import get_settings

    monkeypatch.setenv("ZZ2_TROUBLESHOOTING_STORE", "local")
    get_settings.cache_clear()
    KitRepository().save(KIT, {"vehicle": "Mercedes NTG3"})
    return app


def record(client, *, who=None, path=None, **fields):
    body = {"kit_key": KIT, "path": path or PATH_A, **fields}
    return client.post("/api/troubleshooting/sessions", json=body,
                       headers=auth_header(**(who or TECH)))


class TestRecording:
    def test_session_is_stored_under_the_callers_name(self, local_app):
        response = record(local_app, checked_steps=["DIP 3 ON"], ticket_number="7001",
                          started_at="2026-11-16T10:00:00Z")
        assert response.status_code == 201
        session = response.json()["session"]
        assert session["technician"] == "ana@zz2.com.br"
        assert session["technician_name"] == "Ana"
        assert session["outcome"] == "pending"
        assert session["ticket_number"] == "TKT-7001"
        assert session["duration_seconds"] >= 0

        fetched = local_app.get(f"/api/troubleshooting/sessions/{session['id']}",
                                headers=auth_header(**TECH))
        assert fetched.json()["session"] == session

    def test_technician_cannot_be_supplied_by_the_client(self, local_app):
        response = record(local_app, technician="someone-else@zz2.com.br")
        assert response.status_code == 422

    def test_unknown_kit_is_refused(self, local_app):
        response = local_app.post("/api/troubleshooting/sessions",
                                  json={"kit_key": "NOPE", "path": PATH_A},
                                  headers=auth_header(**TECH))
        assert response.status_code == 404

    def test_empty_path_is_refused(self, local_app):
        assert record(local_app, path=[" "]).status_code == 400

    def test_requires_create_tickets(self, local_app):
        response = local_app.post("/api/troubleshooting/sessions",
                                  json={"kit_key": KIT, "path": PATH_A},
                                  headers=auth_header(role="nobody"))
        assert response.status_code == 403


class TestAmending:
    def test_owner_closes_the_session_and_the_outcome_is_then_final(self, local_app):
        session_id = record(local_app).json()["session"]["id"]
        url = f"/api/troubleshooting/sessions/{session_id}"

        closed = local_app.patch(url, json={"outcome": "resolved", "ticket_number": "TKT-7002"},
                                 headers=auth_header(**TECH))
        assert closed.status_code == 200
        assert closed.json()["session"]["outcome"] == "resolved"
        assert closed.json()["session"]["ticket_number"] == "TKT-7002"

        reopened = local_app.patch(url, json={"outcome": "unresolved"},
                                   headers=auth_header(**TECH))
        assert reopened.status_code == 409

    def test_another_technician_cannot_change_it(self, local_app):
        session_id = record(local_app).json()["session"]["id"]
        response = local_app.patch(f"/api/troubleshooting/sessions/{session_id}",
                                   json={"outcome": "resolved"},
                                   headers=auth_header(**OTHER_TECH))
        assert response.status_code == 403

    def test_a_kit_editor_can(self, local_app):
        session_id = record(local_app).json()["session"]["id"]
        response = local_app.patch(f"/api/troubleshooting/sessions/{session_id}",
                                   json={"notes": "Confirmed by phone"},
                                   headers=auth_header(**EDITOR))
        assert response.status_code == 200

    def test_unknown_session_is_404(self, local_app):
        response = local_app.get("/api/troubleshooting/sessions/abcdef0123456789",
                                 headers=auth_header(**TECH))
        assert response.status_code == 404


class TestListingAndStats:
    def test_filters_and_pagination(self, local_app):
        for _ in range(3):
            record(local_app, outcome="resolved")
        record(local_app, who=OTHER_TECH, path=PATH_B, outcome="unresolved",
               ticket_number="TKT-9")

        headers = auth_header(**TECH)
        page = local_app.get("/api/troubleshooting/sessions?limit=2", headers=headers).json()
        assert len(page["sessions"]) == 2 and page["has_more"] is True

        by_tech = local_app.get("/api/troubleshooting/sessions",
                                params={"technician": "bruno@zz2.com.br"},
                                headers=headers).json()["sessions"]
        assert [s["outcome"] for s in by_tech] == ["unresolved"]

        by_ticket = local_app.get("/api/troubleshooting/sessions", params={"ticket": "9"},
                                  headers=headers).json()["sessions"]
        assert len(by_ticket) == 1

        resolved = local_app.get("/api/troubleshooting/sessions",
                                 params={"outcome": "resolved"}, headers=headers).json()
        assert len(resolved["sessions"]) == 3

    def test_stats_rank_the_paths_that_resolve(self, local_app):
        for _ in range(3):
            record(local_app, path=PATH_A, outcome="resolved")
        record(local_app, path=PATH_B, outcome="unresolved")
        record(local_app, path=PATH_B)  # pending: counted, but not in the rate
        record(local_app, path=PATH_B, outcome="abandoned")  # superseded: not in the rate

        stats = local_app.get("/api/troubleshooting/stats", params={"kit": KIT},
                              headers=auth_header(**EDITOR)).json()
        [kit] = stats["kits"]
        assert kit["total"] == 6
        assert kit["outcomes"]["pending"] == 1
        assert kit["outcomes"]["abandoned"] == 1
        assert kit["resolution_rate"] == 0.75
        assert kit["top_resolving_paths"] == [{"path": " > ".join(PATH_A), "count": 3}]
        assert kit["top_failing_paths"] == [{"path": " > ".join(PATH_B), "count": 1}]

    def test_stats_require_analytics(self, local_app):
        response = local_app.get("/api/troubleshooting/stats", headers=auth_header(**TECH))
        assert response.status_code == 403


class TestDatabaseStore:
    @pytest.fixture
    def remote_app(self, app, monkeypatch):
        from zz2.repositories.kits import KitRepository
        from zz2.settings import get_settings

        monkeypatch.setenv("ZZ2_TROUBLESHOOTING_STORE", "database")
        get_settings.cache_clear()
        KitRepository().save(KIT, {})
        return app

    def test_insert_goes_to_the_sessions_table(self, remote_app, fake_db):
        assert record(remote_app).status_code == 201
        [(sql, params)] = fake_db.statements("INSERT INTO troubleshooting_sessions")
        columns = sql.split("(", 1)[1].split(")", 1)[0].split(", ")
        inserted = dict(zip(columns, params, strict=True))
        assert inserted["kit_key"] == KIT
        assert inserted["technician"] == "ana@zz2.com.br"
        assert "%s" in sql and KIT not in sql  # values are bound, never inlined

    def test_listing_is_filtered_server_side(self, remote_app, fake_db):
        remote_app.get("/api/troubleshooting/sessions",
                       params={"kit": KIT, "outcome": "resolved", "limit": 10},
                       headers=auth_header(**TECH))
        [(sql, params)] = [c for c in fake_db.statements("FROM troubleshooting_sessions")
                           if "ORDER BY created_at DESC" in c[0]]
        assert "kit_key = %s" in sql
        assert "outcome = %s" in sql
        assert params[:2] == [KIT, "resolved"]
        assert params[-2:] == [11, 0]  # one extra row answers "is there another page"

    def test_rows_are_read_back_with_iso_timestamps(self, remote_app, fake_db):
        import datetime as dt

        stamp = dt.datetime(2026, 11, 16, 10, 0, tzinfo=dt.UTC)
        fake_db.stub("FROM troubleshooting_sessions WHERE id = %s", [{
            "id": "abc12345", "kit_key": KIT, "path": PATH_A, "checked_steps": [],
            "outcome": "pending", "ticket_number": "", "notes": "",
            "technician": "ana@zz2.com.br", "technician_name": "Ana",
            "started_at": None, "created_at": stamp, "updated_at": stamp,
            "duration_seconds": None,
        }])
        response = remote_app.get("/api/troubleshooting/sessions/abc12345",
                                  headers=auth_header(**TECH))
        assert response.status_code == 200
        assert response.json()["session"]["created_at"] == "2026-11-16T10:00:00Z"

    def test_auto_mode_uses_the_database_when_configured(self, app, monkeypatch, fake_db):
        from zz2.repositories.troubleshooting_sessions import (
            DatabaseSessionRepository,
            LocalSessionRepository,
            session_store,
        )
        from zz2.settings import get_settings

        monkeypatch.setenv("ZZ2_TROUBLESHOOTING_STORE", "auto")
        get_settings.cache_clear()
        assert isinstance(session_store(), DatabaseSessionRepository)
        fake_db.configured = False
        assert isinstance(session_store(), LocalSessionRepository)
