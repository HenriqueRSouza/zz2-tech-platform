"""End-to-end flows through the HTTP layer."""

from __future__ import annotations

import pytest

from tests.conftest import auth_header


def _inserted(statement: tuple[str, list]) -> dict:
    """Column -> value for a recorded ``INSERT INTO t (cols) VALUES (...)``."""
    sql, params = statement
    columns = sql.split("(", 1)[1].split(")", 1)[0].split(", ")
    return dict(zip(columns, params, strict=True))


class TestTicketsWithoutTheSheet:
    """``ZZ2_ENABLE_APPSCRIPT_SYNC=false``: the database is the only shared copy.

    A fresh installation has no store to number from. With the sheet enabled the
    sheet assigns the number; without it, that call can only fail, and the ticket
    never reached the database.
    """

    @pytest.fixture
    def sheetless(self, monkeypatch, settings_env, app, fake_db):
        from zz2.settings import get_settings

        monkeypatch.setenv("ZZ2_ENABLE_APPSCRIPT_SYNC", "false")
        get_settings.cache_clear()
        fake_db.stub("SELECT ticket_number FROM tickets", [{"ticket_number": "TKT-41"}])
        return app

    def test_first_ticket_is_numbered_from_the_database(self, sheetless, fake_db) -> None:
        from zz2.repositories.tickets import TicketStoreRepository

        assert not TicketStoreRepository().exists()
        response = sheetless.post(
            "/api/tickets", json={"client": "First Shop", "product": "ITZ-NTG3"},
            headers=auth_header(permissions=["create_tickets"]),
        )
        assert response.status_code == 201
        assert response.json()["ticketNum"] == "42"

        inserts = fake_db.statements("INSERT INTO tickets")
        assert [_inserted(insert)["ticket_number"] for insert in inserts] == ["TKT-42"]
        assert TicketStoreRepository().exists()

    def test_nothing_is_queued_for_the_sheet(self, sheetless) -> None:
        from zz2.repositories.write_queue import PendingWriteRepository

        sheetless.post("/api/tickets", json={"client": "Shop", "product": "X"},
                       headers=auth_header(permissions=["create_tickets"]))
        assert PendingWriteRepository().all() == []


class TestTicketLifecycle:
    """Create, read, edit and discard, through the API."""

    @pytest.fixture
    def seeded(self, app, fake_db):
        """A store with three tickets and the database answering ticket queries."""
        from zz2.repositories.tickets import TicketStoreRepository

        TicketStoreRepository().replace_all([
            {"ticket": "7001", "date": "2026-06-01", "client": "Multisom",
             "product": "ITZ-GX1-A", "tech": "ana", "status": ""},
            {"ticket": "7002", "date": "2026-06-02", "client": "Som e Cia",
             "product": "IT3-EVO", "tech": "bruno", "status": "Resolved"},
            {"ticket": "7003", "date": "2026-06-03", "client": "Silva",
             "product": "ITZ-GX1-A", "tech": "ana", "status": "RMA"},
        ], source="test")
        fake_db.stub("SELECT ticket_number FROM tickets", [])
        return app

    def test_create_writes_to_the_database_before_acknowledging(self, seeded,
                                                               fake_db) -> None:
        """The database is authoritative and synchronous; the sheet is queued."""
        response = seeded.post(
            "/api/tickets",
            json={"client": "New Shop", "product": "ITZ-GX1-A", "tech": "ana",
                  "issue": "No audio"},
            headers=auth_header(permissions=["create_tickets"]),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["ok"] is True
        assert body["ticketNum"] == "7004"
        assert body["queued"] is True

        inserts = fake_db.statements("INSERT INTO tickets")
        assert len(inserts) == 1
        assert _inserted(inserts[0])["ticket_number"] == "TKT-7004"

    def test_create_queues_the_sheet_write(self, seeded) -> None:
        from zz2.repositories.write_queue import PendingWriteRepository

        seeded.post("/api/tickets", json={"client": "Queued Co", "product": "X"},
                    headers=auth_header(permissions=["create_tickets"]))

        queued = PendingWriteRepository().all()
        assert len(queued) == 1
        assert queued[0].action == "newticket"
        assert queued[0].predicted_number == 7004

    def test_create_increments_the_technician_counter(self, seeded) -> None:
        from zz2.repositories.stats import StatsRepository

        seeded.post("/api/tickets",
                    json={"client": "Counted Co", "product": "X", "tech": "Ana"},
                    headers=auth_header(permissions=["create_tickets"]))
        assert StatsRepository().all()["ana"]["total"] == 1

    def test_create_requires_a_client_or_a_product(self, seeded) -> None:
        response = seeded.post("/api/tickets", json={"tech": "ana"},
                               headers=auth_header(permissions=["create_tickets"]))
        assert response.status_code == 400
        assert "client or an item" in response.json()["error"]

    def test_create_aborts_when_the_database_refuses(self, seeded, fake_db) -> None:
        """No sheet write is queued, so the three copies stay consistent."""
        from zz2.core.errors import UpstreamError
        from zz2.repositories.write_queue import PendingWriteRepository

        fake_db.stub("INSERT INTO tickets", UpstreamError(log_detail="simulated failure"))

        response = seeded.post("/api/tickets", json={"client": "Doomed Co"},
                               headers=auth_header(permissions=["create_tickets"]))
        assert response.status_code == 502
        assert PendingWriteRepository().count() == 0

        from zz2.repositories.tickets import TicketStoreRepository

        numbers = [row["ticket"] for row in TicketStoreRepository().rows()]
        assert numbers == ["7001", "7002", "7003"]

    def test_created_ticket_is_stored_once_and_dated(self, seeded) -> None:
        """Numbering writes the row; a second append duplicated it, undated."""
        import datetime as dt

        from zz2.repositories.tickets import TicketStoreRepository

        seeded.post("/api/tickets", json={"client": "Once Co", "product": "X"},
                    headers=auth_header(permissions=["create_tickets"]))

        rows = [row for row in TicketStoreRepository().rows() if row["ticket"] == "7004"]
        assert len(rows) == 1
        assert rows[0]["date"] == dt.date.today().isoformat()

    def test_edit_keeps_the_original_creation_date(self, seeded) -> None:
        """``to_store()`` would otherwise overwrite it with the form's value."""
        from zz2.repositories.tickets import TicketStoreRepository

        response = seeded.patch(
            "/api/tickets/7002",
            json={"client": "Som e Cia", "product": "IT3-EVO",
                  "status": "Resolved", "resolution": "Replaced harness"},
            headers=auth_header(permissions=["create_tickets"]),
        )
        assert response.status_code == 200

        stored = TicketStoreRepository().find("7002")
        assert stored.date == "2026-06-02"
        assert stored.resolution == "Replaced harness"

    def test_edit_does_not_rewrite_identity_columns(self, seeded, fake_db) -> None:
        seeded.patch("/api/tickets/7002", json={"status": "Resolved"},
                     headers=auth_header(permissions=["create_tickets"]))
        [(sql, params)] = fake_db.statements("UPDATE tickets")
        assigned = sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
        assert "ticket_number" not in assigned
        assert "ticket_date" not in assigned
        assert sql.endswith("WHERE ticket_number = %s")
        assert params[-1] == "TKT-7002"

    def test_delete_is_parameterised(self, seeded, fake_db) -> None:
        seeded.delete("/api/tickets/7003",
                      headers=auth_header(permissions=["create_tickets"]))
        assert fake_db.statements("DELETE FROM tickets") == [
            ("DELETE FROM tickets WHERE ticket_number = %s", ["TKT-7003"])
        ]

    def test_delete_removes_the_row_and_queues_the_sheet_delete(self, seeded) -> None:
        from zz2.repositories.tickets import TicketStoreRepository
        from zz2.repositories.write_queue import PendingWriteRepository

        response = seeded.delete("/api/tickets/7003",
                                 headers=auth_header(permissions=["create_tickets"]))
        assert response.status_code == 200
        assert TicketStoreRepository().find("7003") is None
        assert PendingWriteRepository().pending_deletes() == {7003}

    def test_search_finds_matches_across_fields(self, seeded) -> None:
        response = seeded.get("/api/tickets/search?q=multisom",
                              headers=auth_header(permissions=["tickets"]))
        assert response.status_code == 200
        results = response.json()["results"]
        assert len(results) == 1
        assert results[0]["ticketNum"] == "7001"

    def test_search_returns_nothing_for_an_empty_query(self, seeded) -> None:
        body = seeded.get("/api/tickets/search?q=",
                          headers=auth_header(permissions=["tickets"])).json()
        assert body["results"] == []
        assert body["scanned"] == 3

    def test_search_limit_is_a_scan_window(self, seeded) -> None:
        """The dropdown reads "Scan last N tickets"."""
        body = seeded.get("/api/tickets/search?q=a&limit=1",
                          headers=auth_header(permissions=["tickets"])).json()
        assert body["scanned"] == 1

    def test_get_one_ticket(self, seeded) -> None:
        response = seeded.get("/api/tickets/7001", headers=auth_header())
        assert response.status_code == 200
        assert response.json()["result"]["client"] == "Multisom"

    def test_get_hides_a_ticket_whose_delete_is_in_flight(self, seeded) -> None:
        seeded.delete("/api/tickets/7001",
                      headers=auth_header(permissions=["create_tickets"]))
        assert seeded.get("/api/tickets/7001", headers=auth_header()).status_code == 404

    def test_recent_merges_local_and_remote(self, seeded, fake_db) -> None:
        """Database rows overwrite local ones by number; local-only rows survive."""
        import datetime as dt

        fake_db.stub("WHERE ticket_date >= %s", [
            {"ticket_number": "TKT-7002", "ticket_date": dt.date(2026, 6, 2),
             "client": "Som e Cia (authoritative)", "status": "Resolved"},
            {"ticket_number": "TKT-9001", "ticket_date": dt.date(2026, 6, 4),
             "client": "Created In The Database", "status": None},
        ])
        body = seeded.get("/api/tickets/recent?days=30", headers=auth_header()).json()

        assert body["source"] == "database"
        by_number = {row["ticketNum"]: row for row in body["results"]}
        assert by_number["TKT-7002"]["client"] == "Som e Cia (authoritative)"
        assert "TKT-9001" in by_number
        assert by_number["TKT-9001"]["date"] == "2026-06-04"
        assert "7001" in by_number, "local-only rows must not disappear"

    def test_recent_falls_back_to_local_when_the_database_is_down(
        self, seeded, fake_db
    ) -> None:
        from zz2.core.errors import UpstreamError

        fake_db.stub("WHERE ticket_date >= %s", UpstreamError(log_detail="down"))

        body = seeded.get("/api/tickets/recent", headers=auth_header()).json()
        assert body["source"] == "local_fallback"
        assert body["results"]


class TestUserAdministration:
    @pytest.fixture
    def admin_header(self, fake_admin):
        fake_admin.seed(email="root@zz2.com.br", role="super_admin",
                        display="Root")
        fake_admin.seed(email="tech@zz2.com.br", role="tech", display="Tech")
        return auth_header(email="admin@zz2.com.br", role="admin",
                           permissions=["manage_users"])

    def test_directory_lists_accounts_with_capability_flags(self, app, admin_header) -> None:
        body = app.get("/api/users", headers=admin_header).json()
        assert body["ok"] is True

        by_email = {user["username"]: user for user in body["users"]}
        assert by_email["tech@zz2.com.br"]["manageable"] is True
        # An admin cannot manage a super-admin: higher rank.
        assert by_email["root@zz2.com.br"]["manageable"] is False

    def test_admin_cannot_create_a_super_admin(self, app, admin_header) -> None:
        response = app.post("/api/users", json={
            "email": "new@zz2.com.br", "display": "New",
            "password": "a-long-enough-password", "role": "super_admin",
        }, headers=admin_header)
        assert response.status_code == 403

    def test_admin_can_create_a_tech(self, app, admin_header, fake_admin) -> None:
        response = app.post("/api/users", json={
            "email": "new@zz2.com.br", "display": "New Tech",
            "password": "a-long-enough-password", "role": "tech",
        }, headers=admin_header)
        assert response.status_code == 201
        assert any(user["email"] == "new@zz2.com.br" for user in fake_admin.users)

    def test_short_passwords_are_rejected(self, app, admin_header) -> None:
        response = app.post("/api/users", json={
            "email": "new@zz2.com.br", "display": "New",
            "password": "short", "role": "tech",
        }, headers=admin_header)
        assert response.status_code == 422

    def test_duplicate_email_is_reported_clearly(self, app, admin_header) -> None:
        response = app.post("/api/users", json={
            "email": "tech@zz2.com.br", "display": "Dup",
            "password": "a-long-enough-password", "role": "tech",
        }, headers=admin_header)
        assert response.status_code == 502
        assert "already exists" in response.json()["error"]

    def test_extra_grants_are_capped_at_what_the_actor_holds(
        self, app, admin_header, fake_admin
    ) -> None:
        """Filtered rather than rejected — the form sends the full checkbox
        state, which can include role-inherited permissions."""
        app.post("/api/users", json={
            "email": "capped@zz2.com.br", "display": "Capped",
            "password": "a-long-enough-password", "role": "tech",
            "permissions": ["manage_users", "system_settings", "analytics"],
        }, headers=admin_header)

        created = next(u for u in fake_admin.users if u["email"] == "capped@zz2.com.br")
        granted = set(created["app_metadata"]["permissions"])
        assert "system_settings" not in granted, "escalation: actor lacks it"
        assert "manage_users" in granted, "actor holds this one"

    def test_the_last_active_root_cannot_be_deleted(self, app, fake_admin) -> None:
        """Losing it is unrecoverable from inside the application."""
        root_id = fake_admin.seed(email="onlyroot@zz2.com.br", role="super_admin")
        header = auth_header(email="other@zz2.com.br", role="super_admin",
                             permissions=["manage_users"])

        response = app.delete(f"/api/users/{root_id}", headers=header)
        assert response.status_code == 409
        assert "last active super-admin" in response.json()["error"]

    def test_a_second_root_makes_the_first_deletable(self, app, fake_admin) -> None:
        first = fake_admin.seed(email="root1@zz2.com.br", role="super_admin")
        fake_admin.seed(email="root2@zz2.com.br", role="super_admin")
        header = auth_header(email="root2@zz2.com.br", role="super_admin",
                             permissions=["manage_users"])

        assert app.delete(f"/api/users/{first}", headers=header).status_code == 200

    def test_a_user_cannot_delete_themselves(self, app, fake_admin) -> None:
        own_id = fake_admin.seed(email="self@zz2.com.br", role="admin")
        fake_admin.seed(email="root@zz2.com.br", role="super_admin")
        header = auth_header(email="self@zz2.com.br", role="admin",
                             permissions=["manage_users"])

        response = app.delete(f"/api/users/{own_id}", headers=header)
        assert response.status_code == 400

    def test_deactivating_a_user_bans_the_account(self, app, fake_admin) -> None:
        target = fake_admin.seed(email="leaver@zz2.com.br", role="tech")
        header = auth_header(email="admin@zz2.com.br", role="admin",
                             permissions=["manage_users"])

        response = app.patch(f"/api/users/{target}", json={"active": False},
                             headers=header)
        assert response.status_code == 200

        account = next(u for u in fake_admin.users if u["id"] == target)
        assert account["banned_until"] is not None

    def test_role_and_permissions_are_always_written_together(
        self, app, fake_admin
    ) -> None:
        """Supabase replaces ``app_metadata`` wholesale, so sending only the role
        would clear every extra grant."""
        target = fake_admin.seed(email="t@zz2.com.br", role="tech",
                                 permissions=["analytics"])
        header = auth_header(email="admin@zz2.com.br", role="admin",
                             permissions=["manage_users", "analytics"])

        app.patch(f"/api/users/{target}", json={"role": "tech"}, headers=header)

        account = next(u for u in fake_admin.users if u["id"] == target)
        assert account["app_metadata"]["permissions"] == ["analytics"]


class TestRoleAdministration:
    @pytest.fixture
    def admin_header(self, fake_admin):
        return auth_header(
            email="admin@zz2.com.br", role="admin",
            permissions=["manage_roles", "tickets", "dashboard", "analytics"],
        )

    def test_listing_reports_editability(self, app, admin_header) -> None:
        body = app.get("/api/roles", headers=admin_header).json()
        by_key = {role["key"]: role for role in body["roles"]}

        assert by_key["super_admin"]["editable"] is False
        assert by_key["tech"]["editable"] is True
        assert by_key["tech"]["deletable"] is False, "built-in roles are permanent"
        assert body["max_assignable_rank"] == 49

    def test_creating_a_role_below_the_actor_succeeds(self, app, admin_header) -> None:
        response = app.post("/api/roles", json={
            "key": "auditor", "label": "Auditor", "rank": 20,
            "permissions": ["tickets", "analytics"],
        }, headers=admin_header)
        assert response.status_code == 201

    def test_a_role_cannot_carry_permissions_the_actor_lacks(
        self, app, admin_header
    ) -> None:
        """Rejected, not filtered: the request *is* the permission set."""
        response = app.post("/api/roles", json={
            "key": "sneaky", "label": "Sneaky", "rank": 20,
            "permissions": ["system_settings"],
        }, headers=admin_header)
        assert response.status_code == 403

    def test_a_role_cannot_be_created_at_or_above_the_actor_rank(
        self, app, admin_header
    ) -> None:
        response = app.post("/api/roles", json={
            "key": "peer", "label": "Peer", "rank": 50, "permissions": ["tickets"],
        }, headers=admin_header)
        assert response.status_code == 400

    def test_root_rank_is_unreachable(self, app) -> None:
        header = auth_header(email="root@zz2.com.br", role="super_admin")
        response = app.post("/api/roles", json={
            "key": "godmode", "label": "God", "rank": 100, "permissions": [],
        }, headers=header)
        assert response.status_code == 422

    def test_a_role_in_use_cannot_be_deleted(self, app, admin_header, fake_admin) -> None:
        app.post("/api/roles", json={"key": "inuse", "label": "In Use", "rank": 20,
                                     "permissions": ["tickets"]},
                 headers=admin_header)
        fake_admin.seed(email="holder@zz2.com.br", role="inuse")

        response = app.delete("/api/roles/inuse", headers=admin_header)
        assert response.status_code == 409
        assert "Reassign" in response.json()["error"]

    def test_an_unused_custom_role_can_be_deleted(self, app, admin_header) -> None:
        app.post("/api/roles", json={"key": "unused", "label": "Unused", "rank": 20,
                                     "permissions": ["tickets"]},
                 headers=admin_header)
        assert app.delete("/api/roles/unused", headers=admin_header).status_code == 200

    def test_built_in_roles_cannot_be_deleted(self, app, admin_header) -> None:
        assert app.delete("/api/roles/tech", headers=admin_header).status_code == 403

    def test_a_system_role_keeps_its_rank_on_edit(self, app, admin_header) -> None:
        """Their ranks are what the hierarchy is calibrated against."""
        from zz2.repositories.roles import RoleRepository

        app.patch("/api/roles/tech", json={"label": "Technician", "rank": 45},
                  headers=admin_header)

        role = RoleRepository().by_key("tech")
        assert role.label == "Technician"
        assert role.rank == 10


class TestKitEditing:
    @pytest.fixture
    def editor(self, app):
        return auth_header(role="editor", permissions=["kit_editor"])

    def test_create_save_and_history(self, app, editor) -> None:
        assert app.post("/api/kit/new", json={
            "key": "ITZ-TEST", "fields": {"vehicle": "Test Car 2026"},
        }, headers=editor).status_code == 201

        response = app.post("/api/kit/save", json={
            "key": "ITZ-TEST",
            "fields": {"vehicle": "Test Car 2027", "notes": "Updated"},
            "troubleshooting": [{"parent": "", "issue": "No power", "steps": ["Check fuse"]}],
        }, headers=editor)
        assert response.status_code == 200

        catalog = app.get("/api/catalog", headers=editor).json()
        assert catalog["kits"]["ITZ-TEST"]["vehicle"] == "Test Car 2027"
        assert len(catalog["troubleshooting"]["ITZ-TEST"]) == 1

        # The pre-save state was snapshotted, so the edit is reversible.
        versions = app.get("/api/kit/history?key=ITZ-TEST", headers=editor).json()
        assert len(versions["versions"]) == 1

    def test_duplicate_key_is_a_conflict(self, app, editor) -> None:
        app.post("/api/kit/new", json={"key": "ITZ-DUP"}, headers=editor)
        response = app.post("/api/kit/new", json={"key": "ITZ-DUP"}, headers=editor)
        assert response.status_code == 409

    def test_saving_an_unknown_kit_is_a_404(self, app, editor) -> None:
        response = app.post("/api/kit/save", json={"key": "ITZ-NOPE"}, headers=editor)
        assert response.status_code == 404

    def test_partial_save_leaves_the_tree_alone(self, app, editor) -> None:
        """``troubleshooting`` omitted means "unchanged", not "empty"."""
        app.post("/api/kit/new", json={"key": "ITZ-PART"}, headers=editor)
        app.post("/api/kit/save", json={
            "key": "ITZ-PART",
            "troubleshooting": [{"parent": "", "issue": "Keep me", "steps": []}],
        }, headers=editor)

        app.post("/api/kit/save", json={"key": "ITZ-PART",
                                        "fields": {"notes": "just a note"}},
                 headers=editor)

        catalog = app.get("/api/catalog", headers=editor).json()
        assert len(catalog["troubleshooting"]["ITZ-PART"]) == 1

    def test_restore_rolls_back_to_a_snapshot(self, app, editor) -> None:
        app.post("/api/kit/new", json={"key": "ITZ-ROLL",
                                       "fields": {"vehicle": "Original"}},
                 headers=editor)
        app.post("/api/kit/save", json={"key": "ITZ-ROLL",
                                        "fields": {"vehicle": "Changed"}},
                 headers=editor)

        version = app.get("/api/kit/history?key=ITZ-ROLL",
                          headers=editor).json()["versions"][0]
        assert app.post("/api/kit/restore",
                        json={"key": "ITZ-ROLL", "version": version},
                        headers=editor).status_code == 200

        catalog = app.get("/api/catalog", headers=editor).json()
        assert catalog["kits"]["ITZ-ROLL"]["vehicle"] == "Original"

    def test_duplicate_copies_fields_and_tree(self, app, editor) -> None:
        app.post("/api/kit/new", json={"key": "ITZ-SRC",
                                       "fields": {"vehicle": "Source Car"}},
                 headers=editor)
        app.post("/api/kit/save", json={
            "key": "ITZ-SRC",
            "troubleshooting": [{"parent": "", "issue": "Copied", "steps": []}],
        }, headers=editor)

        response = app.post("/api/kit/duplicate",
                            json={"source": "ITZ-SRC", "key": "ITZ-COPY"},
                            headers=editor)
        assert response.status_code == 201

        catalog = app.get("/api/catalog", headers=editor).json()
        assert catalog["kits"]["ITZ-COPY"]["vehicle"] == "Source Car"
        assert catalog["troubleshooting"]["ITZ-COPY"][0]["issue"] == "Copied"


class TestWorkspace:
    def test_drafts_are_isolated_per_caller(self, app) -> None:
        ana = auth_header(email="ana@zz2.com.br", subject="u-ana")
        bruno = auth_header(email="bruno@zz2.com.br", subject="u-bruno")

        app.post("/api/draft", json={"draft": {"client": "Ana's work"}}, headers=ana)
        app.post("/api/draft", json={"draft": {"client": "Bruno's work"}}, headers=bruno)

        assert app.get("/api/draft", headers=ana).json()["draft"]["client"] == "Ana's work"
        assert app.get("/api/draft", headers=bruno).json()["draft"]["client"] == "Bruno's work"

    def test_clearing_a_draft(self, app) -> None:
        header = auth_header(email="ana@zz2.com.br")
        app.post("/api/draft", json={"draft": {"a": 1}}, headers=header)
        app.post("/api/draft", json={"clear": True}, headers=header)
        assert app.get("/api/draft", headers=header).json()["draft"] is None

    def test_counters_are_attributed_to_the_token_not_the_body(self, app) -> None:
        """A body-supplied username would let anyone corrupt a colleague's
        numbers, which the dashboard presents as performance data."""
        header = auth_header(email="ana@zz2.com.br")
        app.post("/api/stats/increment", json={"username": "someone-else"},
                 headers=header)

        stats = app.get("/api/stats",
                        headers=auth_header(permissions=["dashboard"])).json()["stats"]
        assert "ana@zz2.com.br" in stats
        assert "someone-else" not in stats


class TestCompatibilityShims:
    """The legacy URL scheme keeps working, minus its insecure parts."""

    def test_legacy_verify_works_with_a_header(self, app) -> None:
        response = app.post("/verify", headers=auth_header())
        assert response.status_code == 200
        assert response.json()["role"] == "tech"

    def test_legacy_password_login_is_gone(self, app) -> None:
        response = app.post("/login", json={"username": "a", "password": "b"})
        assert response.status_code == 400
        assert "moved" in response.json()["error"]

    def test_legacy_csv_proxy_serves_the_local_catalogue(self, app) -> None:
        response = app.get("/api/proxy?action=csv&sheet=kits", headers=auth_header())
        assert response.status_code == 200
        assert isinstance(response.json(), dict)

    def test_legacy_csv_proxy_now_requires_authentication(self, app) -> None:
        assert app.get("/api/proxy?action=csv&sheet=kits").status_code == 401

    def test_legacy_proxy_refuses_arbitrary_actions(self, app) -> None:
        """The monolith forwarded unknown actions straight to Apps Script, so the
        endpoint's real capability set was whatever the script implemented."""
        response = app.get("/api/proxy?action=export", headers=auth_header())
        assert response.status_code == 400

    def test_legacy_ticket_save_dispatches_on_action(self, app, fake_db) -> None:
        from zz2.repositories.tickets import TicketStoreRepository

        TicketStoreRepository().replace_all([{"ticket": "7001", "client": "X"}],
                                            source="test")
        fake_db.stub("SELECT ticket_number FROM tickets", [])

        response = app.post("/api/proxy", json={
            "action": "newticket", "client": "Legacy Co", "product": "ITZ-X",
        }, headers=auth_header(permissions=["create_tickets"]))
        assert response.status_code == 200
        assert response.json()["ticketNum"] == "7002"

    def test_legacy_mutating_get_becomes_a_redirect(self, app) -> None:
        """A GET that starts a backup must not keep working: anything that
        prefetches it fires one."""
        response = app.get("/api/backup", follow_redirects=False)
        assert response.status_code == 308
        assert response.headers["location"] == "/api/system/backup"


class TestHealthProbes:
    def test_liveness_needs_no_credentials(self, app) -> None:
        response = app.get("/healthz")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_readiness_reports_its_checks(self, app) -> None:
        body = app.get("/readyz").json()
        assert body["checks"]["data_dir_writable"] is True
        assert body["checks"]["database_configured"] is True


class TestSessionContract:
    """The shape the eight pages actually read.

    They were written against the monolith's flat response and read
    ``d.permissions``, ``d.display`` and ``d.role`` at the top level. Breaking
    that shape does not fail loudly — it renders a page with no navigation and no
    name, which looks like a permissions problem.
    """

    def test_verify_returns_identity_both_nested_and_flat(self, app) -> None:
        body = app.post("/api/session/verify",
                        headers=auth_header(role="admin",
                                            permissions=["manage_users"])).json()
        assert body["ok"] is True

        assert body["user"]["role"] == "admin"
        assert body["role"] == "admin"
        assert body["display"] == "Test Tech"
        assert "manage_users" in body["permissions"]

    def test_the_legacy_route_returns_the_same_shape(self, app) -> None:
        canonical = app.post("/api/session/verify", headers=auth_header()).json()
        legacy = app.post("/verify", headers=auth_header()).json()
        assert canonical.keys() == legacy.keys()

    def test_logout_reports_success(self, app) -> None:
        response = app.post("/api/session/logout", headers=auth_header())
        assert response.status_code == 200
        assert response.json()["ok"] is True


class TestProbeMethods:
    def test_health_probes_answer_head(self, app) -> None:
        """Uptime monitors and some load balancers probe with HEAD; a 405 from a
        health endpoint reads as an outage."""
        assert app.head("/healthz").status_code == 200
        assert app.head("/readyz").status_code == 200
