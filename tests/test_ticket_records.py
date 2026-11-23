"""Raw ticket-record endpoints, ticket presence, and the SQL data paths.

Authentication is replaced by a dependency override so these tests exercise the
data layer and the permission checks without depending on the token format.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from zz2.core.errors import UpstreamError


class _DriverError(Exception):
    """Stands in for a psycopg error carrying a SQLSTATE."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _db_error(sqlstate: str) -> UpstreamError:
    error = UpstreamError(log_detail=f"database: {sqlstate}")
    error.__cause__ = _DriverError(sqlstate)
    return error


@pytest.fixture
def as_user(app):
    """Call ``as_user(*permissions, email=...)`` to act as that principal."""
    from zz2.api.deps import current_principal
    from zz2.domain.permissions import Permission, Principal

    def use(*permissions: str, email: str = "tech@zz2.com.br",
            display: str = "Test Tech") -> None:
        principal = Principal(
            subject=email, username=email, display=display, role_key="tech",
            granted=frozenset(Permission(p) for p in permissions),
        )
        app.app.dependency_overrides[current_principal] = lambda: principal

    yield use
    app.app.dependency_overrides.clear()


ROW = {
    "id": 7, "ticket_number": "TKT-7001", "ticket_date": dt.date(2026, 6, 1),
    "tech": "ana", "client": "Multisom", "product": "ITZ-GX1-A", "vehicle": None,
    "issue": "No audio", "resolution": None, "status": "", "serial": None,
    "notes": None, "created_at": dt.datetime(2026, 6, 1, 13, 30, tzinfo=dt.UTC),
}


# -- Listing ----------------------------------------------------------------------
class TestList:
    def test_rows_are_returned_raw_with_iso_dates(self, app, as_user, fake_db) -> None:
        as_user("tickets")
        fake_db.stub("FROM tickets", [ROW])
        body = app.get("/api/ticket-records?limit=50&offset=100").json()

        assert body["ok"] is True
        [row] = body["rows"]
        assert row["ticket_date"] == "2026-06-01"
        assert row["created_at"] == "2026-06-01T13:30:00Z"
        assert row["id"] == 7 and row["vehicle"] is None

        [(sql, params)] = fake_db.statements("FROM tickets")
        assert sql.endswith("ORDER BY ticket_number DESC LIMIT %s OFFSET %s")
        assert params == [50, 100]

    def test_defaults_and_bounds(self, app, as_user, fake_db) -> None:
        as_user("tickets")
        app.get("/api/ticket-records")
        assert fake_db.statements("FROM tickets")[0][1] == [1000, 0]
        assert app.get("/api/ticket-records?limit=0").status_code == 422
        assert app.get("/api/ticket-records?limit=5001").status_code == 422
        assert app.get("/api/ticket-records?offset=-1").status_code == 422

    def test_requires_the_tickets_permission(self, app, as_user) -> None:
        as_user("create_tickets")
        assert app.get("/api/ticket-records").status_code == 403

    def test_requires_authentication(self, app) -> None:
        assert app.get("/api/ticket-records").status_code == 401

    def test_database_outage_is_a_502(self, app, as_user, fake_db) -> None:
        as_user("tickets")
        fake_db.stub("FROM tickets", UpstreamError(log_detail="down"))
        response = app.get("/api/ticket-records")
        assert response.status_code == 502
        assert "down" not in response.text


# -- History ----------------------------------------------------------------------
class TestHistory:
    def test_client_history_is_ordered_and_bound(self, app, as_user, fake_db) -> None:
        as_user("tickets")
        fake_db.stub("ILIKE", [ROW])
        body = app.get("/api/ticket-records/history",
                       params={"field": "client", "pattern": "%multisom%"}).json()
        assert body["rows"][0]["ticket_number"] == "TKT-7001"

        [(sql, params)] = fake_db.statements("ILIKE")
        assert "WHERE client ILIKE %s" in sql
        assert "ORDER BY ticket_date DESC NULLS LAST, ticket_number DESC" in sql
        assert params == ["%multisom%", 1000, 0]

    @pytest.mark.parametrize(("pattern", "expected"), [
        ("*silva*", "%silva%"),          # PostgREST wildcard
        ("%silva%", "%silva%"),          # SQL wildcard
        ("ITZ_100", "ITZ\\_100"),        # underscore is literal
        ("a\\b", "a\\\\b"),              # backslash is literal
        ("'; DROP TABLE tickets; --", "'; DROP TABLE tickets; --"),  # just data
    ])
    def test_pattern_translation(self, app, as_user, fake_db, pattern, expected) -> None:
        as_user("tickets")
        app.get("/api/ticket-records/history",
                params={"field": "product", "pattern": pattern})
        [(sql, params)] = fake_db.statements("ILIKE")
        assert "WHERE product ILIKE %s" in sql
        assert params[0] == expected

    def test_field_is_whitelisted(self, app, as_user, fake_db) -> None:
        as_user("tickets")
        response = app.get("/api/ticket-records/history",
                           params={"field": "notes; --", "pattern": "x"})
        assert response.status_code == 422
        assert fake_db.calls == []

    def test_pattern_is_required(self, app, as_user) -> None:
        as_user("tickets")
        response = app.get("/api/ticket-records/history", params={"field": "client"})
        assert response.status_code == 422


# -- Count ------------------------------------------------------------------------
class TestCount:
    def test_bounds_are_optional_and_bound(self, app, as_user, fake_db) -> None:
        as_user("dashboard")
        fake_db.stub("count(*)", {"count": 12})
        body = app.get("/api/ticket-records/count",
                       params={"since": "2026-11-01", "before": "2026-11-08"}).json()
        assert body == {"ok": True, "count": 12}
        [(sql, params)] = fake_db.statements("count(*)")
        assert sql.endswith("WHERE ticket_date >= %s AND ticket_date < %s")
        assert params == [dt.date(2026, 11, 1), dt.date(2026, 11, 8)]

    def test_no_bounds_counts_everything(self, app, as_user, fake_db) -> None:
        as_user("tickets")
        fake_db.stub("count(*)", {"count": 3})
        assert app.get("/api/ticket-records/count").json()["count"] == 3
        [(sql, params)] = fake_db.statements("count(*)")
        assert "WHERE" not in sql and params == []

    def test_invalid_date_is_rejected(self, app, as_user) -> None:
        as_user("tickets")
        assert app.get("/api/ticket-records/count?since=yesterday").status_code == 422

    def test_needs_dashboard_or_tickets(self, app, as_user) -> None:
        as_user("create_tickets", "analytics")
        assert app.get("/api/ticket-records/count").status_code == 403


# -- Create -----------------------------------------------------------------------
def _columns(sql: str) -> list[str]:
    return sql.split("(", 1)[1].split(")", 1)[0].split(", ")


class TestCreate:
    def test_inserts_only_the_supplied_columns(self, app, as_user, fake_db) -> None:
        as_user("create_tickets")
        fake_db.stub("INSERT INTO tickets", [ROW])
        response = app.post("/api/ticket-records", json={
            "ticket_number": "TKT-7001", "client": "  Multisom ", "issue": "No audio",
        })
        assert response.status_code == 201
        assert response.json()["row"]["ticket_date"] == "2026-06-01"

        [(sql, params)] = fake_db.statements("INSERT INTO tickets")
        assert dict(zip(_columns(sql), params, strict=True)) == {
            "ticket_number": "TKT-7001", "client": "Multisom", "issue": "No audio"}
        assert "RETURNING id, ticket_number" in sql

    def test_explicit_date_is_kept_and_null_date_uses_the_default(
            self, app, as_user, fake_db) -> None:
        as_user("create_tickets")
        fake_db.stub("INSERT INTO tickets", [ROW])
        app.post("/api/ticket-records",
                 json={"ticket_number": "TKT-1", "ticket_date": "2026-05-04"})
        app.post("/api/ticket-records", json={"ticket_number": "TKT-2", "ticket_date": None})
        first, second = fake_db.statements("INSERT INTO tickets")
        assert dict(zip(_columns(first[0]), first[1], strict=True)) == {
            "ticket_number": "TKT-1", "ticket_date": dt.date(2026, 5, 4)}
        assert "ticket_date" not in _columns(second[0])

    @pytest.mark.parametrize("body", [
        {"ticket_number": "TKT-1", "id": 99},
        {"ticket_number": "TKT-1", "created_at": "2020-01-01T00:00:00Z"},
        {"ticket_number": "TKT-1", "client; DROP TABLE tickets": "x"},
        {"client": "no number"},
        {"ticket_number": "   "},
        {"ticket_number": "TKT-1", "notes": "x" * 10_001},
        {"ticket_number": "TKT-1", "ticket_date": "not a date"},
    ])
    def test_rejects_anything_off_the_whitelist(self, app, as_user, fake_db, body) -> None:
        as_user("create_tickets")
        assert app.post("/api/ticket-records", json=body).status_code == 422
        assert fake_db.calls == []

    def test_duplicate_number_is_a_conflict(self, app, as_user, fake_db) -> None:
        as_user("create_tickets")
        fake_db.stub("INSERT INTO tickets", _db_error("23505"))
        response = app.post("/api/ticket-records", json={"ticket_number": "TKT-7001"})
        assert response.status_code == 409

    def test_requires_create_tickets(self, app, as_user) -> None:
        as_user("tickets")
        response = app.post("/api/ticket-records", json={"ticket_number": "TKT-1"})
        assert response.status_code == 403


# -- Patch ------------------------------------------------------------------------
class TestPatch:
    def test_updates_only_the_supplied_columns(self, app, as_user, fake_db) -> None:
        as_user("tickets")
        fake_db.stub("UPDATE tickets", [{**ROW, "status": "Resolved"}])
        response = app.patch("/api/ticket-records/7",
                             json={"status": "Resolved", "resolution": None})
        assert response.status_code == 200
        assert response.json()["row"]["status"] == "Resolved"

        [(sql, params)] = fake_db.statements("UPDATE tickets")
        assignments = sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0].split(", ")
        assert dict(zip(assignments, params, strict=False)) == {
            "status = %s": "Resolved", "resolution = %s": None}
        assert " WHERE id = %s RETURNING " in sql
        assert params[-1] == 7

    @pytest.mark.parametrize("body", [
        {"id": 8},
        {"created_at": "2020-01-01T00:00:00Z"},
        {"ticket_number": None},
        {"ticket_date": None},
        {"unknown": "x"},
    ])
    def test_rejects_protected_or_unknown_columns(self, app, as_user, fake_db, body) -> None:
        as_user("tickets")
        assert app.patch("/api/ticket-records/7", json=body).status_code == 422
        assert fake_db.calls == []

    def test_empty_body_is_rejected(self, app, as_user, fake_db) -> None:
        as_user("tickets")
        assert app.patch("/api/ticket-records/7", json={}).status_code == 400

    def test_unknown_row_is_404(self, app, as_user, fake_db) -> None:
        as_user("tickets")
        assert app.patch("/api/ticket-records/404", json={"status": "x"}).status_code == 404

    def test_non_numeric_id_is_rejected(self, app, as_user) -> None:
        as_user("tickets")
        assert app.patch("/api/ticket-records/abc", json={"status": "x"}).status_code == 422

    def test_renumbering_into_an_existing_number_is_a_conflict(
            self, app, as_user, fake_db) -> None:
        as_user("tickets")
        fake_db.stub("UPDATE tickets", _db_error("23505"))
        response = app.patch("/api/ticket-records/7", json={"ticket_number": "TKT-1"})
        assert response.status_code == 409

    def test_requires_tickets(self, app, as_user) -> None:
        as_user("create_tickets")
        assert app.patch("/api/ticket-records/7", json={"status": "x"}).status_code == 403


# -- Presence (unit) ----------------------------------------------------------------
class _Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def hub():
    from zz2.services.presence import PresenceHub

    clock = _Clock()
    instance = PresenceHub(clock=clock)
    instance.clock = clock  # type: ignore[attr-defined]
    return instance


class TestPresenceHub:
    def test_viewers_are_listed_with_their_join_time(self, hub) -> None:
        hub.heartbeat("7001", user="ana", client_id="a1", name="Ana")
        hub.clock.now += 5
        state = hub.heartbeat("7001", user="bruno", client_id="b1", name="Bruno")
        assert [v["client_id"] for v in state["viewers"]] == ["a1", "b1"]
        assert state["viewers"][0]["since"] == "2027-01-15T08:00:00Z"

    def test_viewer_expires_after_30_seconds_without_heartbeat(self, hub) -> None:
        hub.heartbeat("7001", user="ana", client_id="a1", name="Ana")
        hub.clock.now += 29
        assert len(hub.heartbeat("7001", user="bruno", client_id="b1",
                                 name="Bruno")["viewers"]) == 2
        hub.clock.now += 2  # Ana last seen 31 s ago
        viewers = hub.heartbeat("7001", user="bruno", client_id="b1", name="Bruno")["viewers"]
        assert [v["client_id"] for v in viewers] == ["b1"]

    def test_events_reach_others_once_and_never_the_sender(self, hub) -> None:
        hub.heartbeat("7001", user="ana", client_id="a1", name="Ana")
        hub.heartbeat("7001", user="bruno", client_id="b1", name="Bruno")
        hub.publish("7001", user="ana", client_id="a1", event_type="editing",
                    payload={"column": "notes"})

        assert hub.heartbeat("7001", user="ana", client_id="a1", name="Ana")["events"] == []
        [event] = hub.heartbeat("7001", user="bruno", client_id="b1", name="Bruno")["events"]
        assert event["type"] == "editing"
        assert event["payload"] == {"column": "notes"}
        assert event["client_id"] == "a1" and event["name"] == "Ana"
        assert hub.heartbeat("7001", user="bruno", client_id="b1", name="Bruno")["events"] == []

    def test_events_are_kept_for_60_seconds(self, hub) -> None:
        hub.publish("7001", user="ana", client_id="a1", event_type="saved", payload=None)
        hub.clock.now += 59
        assert len(hub.heartbeat("7001", user="bruno", client_id="b1",
                                 name="Bruno")["events"]) == 1
        hub.publish("7001", user="ana", client_id="a1", event_type="saved", payload=None)
        hub.clock.now += 61
        assert hub.heartbeat("7001", user="carla", client_id="c1",
                             name="Carla")["events"] == []

    def test_tickets_are_isolated(self, hub) -> None:
        hub.heartbeat("7001", user="ana", client_id="a1", name="Ana")
        hub.publish("7001", user="ana", client_id="a1", event_type="x", payload=1)
        state = hub.heartbeat("7002", user="bruno", client_id="b1", name="Bruno")
        assert [v["client_id"] for v in state["viewers"]] == ["b1"]
        assert state["events"] == []

    def test_leave_removes_only_the_callers_own_tab(self, hub) -> None:
        hub.heartbeat("7001", user="ana", client_id="shared", name="Ana")
        hub.clock.now += 1
        hub.leave("7001", user="mallory", client_id="shared")
        state = hub.heartbeat("7001", user="bruno", client_id="b1", name="Bruno")
        assert [v["name"] for v in state["viewers"]] == ["Ana", "Bruno"]
        hub.leave("7001", user="ana", client_id="shared")
        state = hub.heartbeat("7001", user="bruno", client_id="b1", name="Bruno")
        assert [v["name"] for v in state["viewers"]] == ["Bruno"]

    def test_is_thread_safe(self, hub) -> None:
        import threading

        def worker(n: int) -> None:
            for i in range(200):
                hub.heartbeat(f"T{i % 7}", user=f"u{n}", client_id=f"c{n}", name="x")
                hub.publish(f"T{i % 7}", user=f"u{n}", client_id=f"c{n}",
                            event_type="e", payload=i)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        state = hub.heartbeat("T0", user="z", client_id="z", name="z")
        assert len(state["viewers"]) == 9


# -- Presence (HTTP) ----------------------------------------------------------------
class TestPresenceApi:
    def test_heartbeat_event_and_leave(self, app, as_user) -> None:
        as_user("tickets", email="ana@zz2.com.br", display="Ana")
        first = app.post("/api/presence/tickets/TKT-7001", json={"client_id": "tab-a"}).json()
        assert first["ok"] is True
        assert first["viewers"][0] == {"client_id": "tab-a", "name": "Ana",
                                       "since": first["viewers"][0]["since"]}
        assert first["events"] == []

        response = app.post("/api/presence/tickets/TKT-7001/events",
                            json={"client_id": "tab-a", "type": "editing",
                                  "payload": {"column": "status"}})
        assert response.json() == {"ok": True}

        as_user("create_tickets", email="bruno@zz2.com.br", display="Bruno")
        state = app.post("/api/presence/tickets/TKT-7001",
                         json={"client_id": "tab-b", "name": "Bruno B."}).json()
        assert [v["name"] for v in state["viewers"]] == ["Ana", "Bruno B."]
        assert [e["type"] for e in state["events"]] == ["editing"]

        assert app.delete("/api/presence/tickets/TKT-7001?client_id=tab-b").json() == {
            "ok": True}
        as_user("tickets", email="ana@zz2.com.br", display="Ana")
        state = app.post("/api/presence/tickets/TKT-7001", json={"client_id": "tab-a"}).json()
        assert [v["client_id"] for v in state["viewers"]] == ["tab-a"]
        assert state["events"] == []  # never echoed to the sender

    @pytest.mark.parametrize("body", [
        {},
        {"client_id": ""},
        {"client_id": "has spaces"},
        {"client_id": "x" * 65},
        {"client_id": "ok", "name": "n" * 121},
        {"client_id": "ok", "extra": 1},
    ])
    def test_heartbeat_validation(self, app, as_user, body) -> None:
        as_user("tickets")
        assert app.post("/api/presence/tickets/7001", json=body).status_code == 422

    def test_event_payload_is_bounded(self, app, as_user) -> None:
        as_user("tickets")
        response = app.post("/api/presence/tickets/7001/events",
                            json={"client_id": "a", "type": "x", "payload": "y" * 9000})
        assert response.status_code == 422

    def test_delete_requires_client_id(self, app, as_user) -> None:
        as_user("tickets")
        assert app.delete("/api/presence/tickets/7001").status_code == 422

    def test_needs_a_ticket_permission(self, app, as_user) -> None:
        as_user("analytics")
        assert app.post("/api/presence/tickets/7001",
                        json={"client_id": "a"}).status_code == 403

    def test_requires_authentication(self, app) -> None:
        assert app.post("/api/presence/tickets/7001",
                        json={"client_id": "a"}).status_code == 401


# -- Gateway helpers and other SQL paths ------------------------------------------------
class TestGatewayHelpers:
    def test_escape_like(self) -> None:
        from zz2.gateways.database import escape_like

        assert escape_like("50%_off\\") == "50\\%\\_off\\\\"

    def test_json_value(self) -> None:
        from zz2.gateways.database import json_row, json_value

        assert json_value(Decimal("3")) == 3 and isinstance(json_value(Decimal("3")), int)
        assert json_value(Decimal("0.25")) == 0.25
        assert json_value(dt.date(2026, 1, 2)) == "2026-01-02"
        local = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone(dt.timedelta(hours=-3)))
        assert json_value(local) == "2026-01-02T06:04:05Z"
        assert json_row(None) is None
        assert json_row({"a": [1]}) == {"a": [1]}

    def test_sqlstate(self) -> None:
        from zz2.gateways.database import sqlstate

        assert sqlstate(_db_error("23505")) == "23505"
        assert sqlstate(UpstreamError()) == ""

    def test_composition_helpers_quote_identifiers(self) -> None:
        from zz2.gateways.database import assignment_list, column_list, placeholder_list

        assert column_list(["a", 'b"c']).as_string(None) == '"a", "b""c"'
        assert placeholder_list(3).as_string(None) == "%s, %s, %s"
        assert assignment_list(["a", "b"]).as_string(None) == '"a" = %s, "b" = %s'


class TestTicketServiceSql:
    def test_history_fuzzy_pattern_escapes_user_text(self, fake_db) -> None:
        from zz2.services.tickets import TicketService

        assert TicketService._history_pattern("100%_Auto", fuzzy=False) == "100\\%\\_Auto"
        assert TicketService._history_pattern("Auto Silva 42", fuzzy=True) == "%42%"

    def test_history_queries_the_database(self, data_dir, fake_db) -> None:
        from zz2.services.tickets import TicketService

        fake_db.stub("ILIKE", [ROW])
        result = TicketService().history(client="Multisom")
        assert result["source"] == "database"
        assert result["results"][0]["date"] == "2026-06-01"
        [(sql, params)] = fake_db.statements("ILIKE")
        assert "WHERE client ILIKE %s" in sql
        assert params[0] == "Multisom"

    def test_history_falls_back_to_the_local_store(self, data_dir, fake_db) -> None:
        from zz2.repositories.tickets import TicketStoreRepository
        from zz2.services.tickets import TicketService

        TicketStoreRepository().replace_all(
            [{"ticket": "1", "client": "Multisom", "date": "2026-01-01"}], source="test")
        fake_db.stub("ILIKE", UpstreamError(log_detail="down"))
        result = TicketService().history(client="Multisom")
        assert result["source"] == "local_fallback"
        assert len(result["results"]) == 1


class TestCustomerServiceSql:
    @pytest.fixture(autouse=True)
    def _clear_caches(self) -> Any:
        from zz2.services import customers

        for cache in (customers._history_cache, customers._summaries_cache,
                      customers._item_sales_cache):
            cache.clear()
        yield

    def test_item_sales_escapes_and_sums(self, fake_db) -> None:
        from zz2.services.customers import CustomerService

        fake_db.stub("FROM sales_by_item", [
            {"quantity_sold": Decimal("2"), "period_start": dt.date(2026, 1, 1)},
            {"quantity_sold": Decimal("3"), "period_start": dt.date(2025, 7, 1)},
        ])
        result = CustomerService().item_sales("ITZ_100%")
        assert result["quantity_sold"] == 5 and result["period_rows"] == 2
        [(sql, params)] = fake_db.statements("FROM sales_by_item")
        assert "item_sku ILIKE %s OR item_name ILIKE %s" in sql
        assert params == ["ITZ\\_100\\%", "ITZ\\_100\\%"]

    def test_history_matches_and_serialises_rows(self, fake_db) -> None:
        from zz2.services.customers import CustomerService

        fake_db.stub("SELECT customer_name FROM customer_performance_semesters WHERE",
                     [{"customer_name": "Auto Multisom Ltda"}])
        fake_db.stub("FROM customer_performance_semesters WHERE customer_name = %s", [
            {"period_year": 2026, "semester": 1, "period_start": dt.date(2026, 1, 1),
             "returns_rate": Decimal("0.2"), "returns_quantity": Decimal("4")},
        ])
        fake_db.stub("FROM customer_performance_months", [])
        result = CustomerService().history("Auto Multisom")
        assert result["ok"] is True
        assert result["matched_customer"] == "Auto Multisom Ltda"
        assert result["alert"] is True
        assert result["semesters"][0]["period_start"] == "2026-01-01"
        assert result["semesters"][0]["returns_rate"] == 0.2
        candidates = fake_db.statements("WHERE customer_name ILIKE %s")
        assert candidates[0][1] == ["%multisom%"]

    def test_summaries_degrade_when_the_database_is_down(self, fake_db) -> None:
        from zz2.services.customers import CustomerService

        fake_db.stub("FROM customer_performance_semesters", UpstreamError(log_detail="x"))
        result = CustomerService().summaries()
        assert result["ok"] is False and result["customers"] == []


class TestMirrorSql:
    def test_upserts_then_prunes_older_generations(self, data_dir, fake_db) -> None:
        from zz2.repositories.kits import KitRepository
        from zz2.services.mirror import MirrorService

        KitRepository().save("ITZ-NTG3", {"vehicle": "Mercedes"})
        counts = MirrorService().upload(reason="test")
        assert counts["kits"] >= 1

        upserts = [call for call in fake_db.calls if call[0].startswith("UPSERT")]
        assert upserts[0][0] == "UPSERT kits"
        deletes = fake_db.statements("DELETE FROM")
        assert [sql.split()[2] for sql, _ in deletes] == [
            "manual_assets", "troubleshooting", "blueprints", "kits"]
        generation = upserts[0][1][0]["sync_generation"]
        assert all(params == [generation] for _, params in deletes)
        assert fake_db.calls.index(upserts[-1]) < fake_db.calls.index(deletes[0])

    def test_no_prune_after_a_failed_upsert(self, data_dir, fake_db, monkeypatch) -> None:
        from zz2.repositories.kits import KitRepository
        from zz2.services.mirror import MirrorService

        KitRepository().save("ITZ-NTG3", {})

        def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise UpstreamError(log_detail="down")

        monkeypatch.setattr(fake_db, "upsert", refuse)
        with pytest.raises(UpstreamError):
            MirrorService().upload()
        assert fake_db.statements("DELETE FROM") == []


class TestReadiness:
    def test_reports_database_configuration(self, app, fake_db) -> None:
        body = app.get("/readyz").json()
        assert body["checks"]["database_configured"] is True
        fake_db.configured = False
        response = app.get("/readyz")
        assert response.status_code == 503
        assert response.json()["checks"]["database_configured"] is False
