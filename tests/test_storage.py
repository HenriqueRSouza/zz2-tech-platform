"""Persistence primitives and repositories."""

from __future__ import annotations

import json
import threading

import pytest

from zz2.core.errors import StorageError
from zz2.core.jsonstore import mutate, read_json, write_json


class TestAtomicWrites:
    def test_round_trip(self, data_dir) -> None:
        path = data_dir / "doc.json"
        write_json(path, {"a": 1})
        assert read_json(path, {}) == {"a": 1}

    def test_missing_file_returns_the_default_quietly(self, data_dir) -> None:
        assert read_json(data_dir / "absent.json", {"fallback": True}) == {"fallback": True}

    def test_written_files_are_not_world_readable(self, data_dir) -> None:
        path = data_dir / "doc.json"
        write_json(path, {})
        assert path.stat().st_mode & 0o007 == 0

    def test_no_temp_file_is_left_beside_the_document(self, data_dir) -> None:
        """The monolith wrote ``<path>.tmp.<pid>`` next to the real file, inside
        the directory its static handler served."""
        path = data_dir / "doc.json"
        write_json(path, {"a": 1})
        siblings = {child.name for child in data_dir.iterdir() if child.is_file()}
        assert siblings == {"doc.json"}

    def test_corrupt_file_is_quarantined_not_silently_replaced(self, data_dir) -> None:
        """``except: return default`` made a truncated file look like a missing
        one, and the next save overwrote whatever survived."""
        path = data_dir / "doc.json"
        path.write_text('{"partial": ', encoding="utf-8")

        assert read_json(path, {"default": True}) == {"default": True}

        quarantined = list(data_dir.glob("doc.json.corrupt*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text(encoding="utf-8") == '{"partial": '

    def test_write_failure_raises(self, data_dir) -> None:
        """A failed save must not look like a successful one. The monolith
        returned ``False`` and most callers ignored it."""
        locked = data_dir / "locked"
        locked.mkdir()
        (locked / "doc.json").write_text("{}", encoding="utf-8")
        locked.chmod(0o500)  # readable and traversable, not writable
        try:
            with pytest.raises(StorageError):
                write_json(locked / "doc.json", {"a": 1})
        finally:
            locked.chmod(0o750)

    def test_an_unusable_path_raises_rather_than_crashing(self, data_dir) -> None:
        """A NUL byte makes os.path.realpath raise ValueError, not OSError.

        No caller should ever produce such a path — every one validates its
        components — but a bug that does must surface as a StorageError and a
        clean 500 rather than an unhandled exception.
        """
        with pytest.raises(StorageError):
            write_json(data_dir / "bad\x00name.json", {})
        with pytest.raises(StorageError):
            read_json(data_dir / "bad\x00name.json", {})


class TestMutate:
    def test_changes_are_persisted(self, data_dir) -> None:
        path = data_dir / "counter.json"
        with mutate(path, {}) as document:
            document["n"] = 1
        assert read_json(path, {}) == {"n": 1}

    def test_an_exception_leaves_the_document_untouched(self, data_dir) -> None:
        path = data_dir / "counter.json"
        write_json(path, {"n": 1})

        with pytest.raises(RuntimeError):
            with mutate(path, {}) as document:
                document["n"] = 999
                raise RuntimeError("boom")

        assert read_json(path, {}) == {"n": 1}

    def test_concurrent_increments_do_not_lose_writes(self, data_dir) -> None:
        """Per-document locking, exercised. The monolith interleaved load and
        save outside its lock on several paths."""
        path = data_dir / "counter.json"
        write_json(path, {"n": 0})

        def bump() -> None:
            for _ in range(50):
                with mutate(path, {"n": 0}) as document:
                    document["n"] = document.get("n", 0) + 1

        threads = [threading.Thread(target=bump) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert read_json(path, {})["n"] == 400

    def test_unrelated_documents_do_not_contend(self, data_dir) -> None:
        """Locks are per file, not global."""
        first, second = data_dir / "a.json", data_dir / "b.json"
        write_json(first, {"n": 0})
        write_json(second, {"n": 0})

        with mutate(first, {}) as document:
            document["n"] = 1
            # Would deadlock under one global lock.
            with mutate(second, {}) as other:
                other["n"] = 2

        assert read_json(first, {})["n"] == 1
        assert read_json(second, {})["n"] == 2


class TestTicketStore:
    @pytest.fixture
    def store(self, data_dir):
        from zz2.repositories.tickets import TicketStoreRepository

        repository = TicketStoreRepository()
        repository.replace_all([
            {"ticket": "7001", "date": "2026-06-01", "client": "Multisom", "status": ""},
            {"ticket": "TKT-7002", "date": "2026-06-02", "client": "Som e Cia",
             "status": "Resolved"},
            {"ticket": 7003, "date": "2026-06-03", "client": "Silva", "status": "RMA"},
        ], source="test")
        return repository

    def test_max_number_reads_across_formats(self, store) -> None:
        """``"7001"``, ``"TKT-7002"`` and ``7003`` are all ticket numbers."""
        assert store.max_number() == 7003

    @pytest.mark.parametrize("query", ["7002", "TKT-7002", " 7002 ", 7002])
    def test_find_matches_on_digits_only(self, store, query) -> None:
        """The bug the monolith's strict string comparison caused: a discarded
        ticket kept reappearing because the delete matched nothing."""
        found = store.find(query)
        assert found is not None
        assert found.client == "Som e Cia"

    @pytest.mark.parametrize("query", ["TKT-7003", "7003", 7003])
    def test_delete_matches_on_digits_only(self, store, query) -> None:
        assert store.delete(query) == 1
        assert store.find("7003") is None
        assert len(store.rows()) == 2

    def test_append_and_number_assigns_the_next_number(self, store) -> None:
        from zz2.domain.models import Ticket

        assigned = store.append_and_number(Ticket(client="New Client"))
        assert assigned == 7004
        assert store.find("7004").client == "New Client"

    def test_append_and_number_respects_a_remote_floor(self, store) -> None:
        """Tickets are also created directly in Supabase, so the local maximum
        alone would reuse their numbers."""
        from zz2.domain.models import Ticket

        assigned = store.append_and_number(Ticket(client="X"), floor=7500)
        assert assigned == 7501

    def test_concurrent_numbering_never_collides(self, store) -> None:
        from zz2.domain.models import Ticket

        assigned: list[int] = []
        lock = threading.Lock()

        def create(index: int) -> None:
            number = store.append_and_number(Ticket(client=f"C{index}"))
            with lock:
                assigned.append(number)

        threads = [threading.Thread(target=create, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(assigned) == 20
        assert len(set(assigned)) == 20, "two tickets claimed the same number"

    def test_merge_new_skips_numbers_already_present(self, store) -> None:
        added = store.merge_new([
            {"ticket": "7002", "client": "duplicate"},
            {"ticket": "7010", "client": "genuinely new"},
        ])
        assert added == 1
        assert store.find("7010").client == "genuinely new"

    def test_replace_window_rebuilds_the_recent_range(self, store) -> None:
        removed = store.replace_window(
            [{"ticket": "7002", "date": "2026-06-02", "client": "Som e Cia LTDA"}],
            floor=7002, protect=set(), suppress=set(),
        )
        assert removed >= 1
        assert store.find("7001") is not None, "older rows must be untouched"
        assert store.find("7002").client == "Som e Cia LTDA"
        assert store.find("7003") is None, "dropped from the sheet's window"

    def test_replace_window_protects_writes_still_in_flight(self, store) -> None:
        """A just-created ticket is legitimately absent from the sheet's answer.
        Dropping it makes a successful save appear to vanish."""
        store.replace_window(
            [{"ticket": "7002", "date": "2026-06-02", "client": "Som e Cia"}],
            floor=7002, protect={7003}, suppress=set(),
        )
        assert store.find("7003") is not None

    def test_replace_window_suppresses_deletes_still_in_flight(self, store) -> None:
        """The sheet still has the row; re-adding it would undo a delete the user
        already saw succeed."""
        store.replace_window(
            [{"ticket": "7002", "date": "2026-06-02", "client": "Som e Cia"},
             {"ticket": "7003", "date": "2026-06-03", "client": "Silva"}],
            floor=7002, protect=set(), suppress={7003},
        )
        assert store.find("7003") is None

    def test_replace_window_ignores_an_empty_authoritative_set(self, store) -> None:
        """A failed or empty fetch means "no information", not "no tickets"."""
        assert store.replace_window([], floor=7000, protect=set(),
                                    suppress=set()) == 0
        assert len(store.rows()) == 3


class TestWriteQueue:
    @pytest.fixture
    def queue(self, data_dir):
        from zz2.repositories.write_queue import PendingWriteRepository

        return PendingWriteRepository()

    def _operation(self, **kwargs):
        from zz2.domain.models import WriteOperation
        from zz2.repositories.write_queue import new_operation_id

        defaults = {"id": new_operation_id(), "action": "newticket", "params": {}}
        return WriteOperation(**{**defaults, **kwargs})

    def test_fifo_order_is_preserved(self, queue) -> None:
        """The sheet assigns numbers by append order, so reordering the queue
        reorders the numbering."""
        first = self._operation(predicted_number=1)
        second = self._operation(predicted_number=2)
        queue.enqueue(first)
        queue.enqueue(second)
        assert queue.head().predicted_number == 1

    def test_remap_repoints_queued_updates_and_deletes(self, queue) -> None:
        """When the sheet assigns a different number, a queued delete left
        pointing at the prediction would delete somebody else's ticket."""
        queue.enqueue(self._operation(action="newticket", predicted_number=100))
        queue.enqueue(self._operation(action="deleteticket",
                                     params={"ticketNum": "100"}))
        queue.enqueue(self._operation(action="updateticket",
                                      params={"ticketNum": "TKT-100"}))

        assert queue.remap_ticket_number(100, 250) == 2

        operations = queue.all()
        assert operations[1].params["ticketNum"] == "250"
        assert operations[2].params["ticketNum"] == "250"

    def test_pending_and_delete_numbers_are_reported(self, queue) -> None:
        queue.enqueue(self._operation(action="newticket", predicted_number=7))
        queue.enqueue(self._operation(action="deleteticket",
                                      params={"ticketNum": "TKT-9"}))
        assert queue.predicted_numbers() == {7}
        assert queue.pending_deletes() == {9}

    def test_dead_letters_survive_a_drain_and_replay(self, data_dir) -> None:
        from zz2.repositories.write_queue import DeadLetterRepository

        dead = DeadLetterRepository()
        dead.add(self._operation(action="newticket", predicted_number=5), "gave up")
        assert dead.count() == 1

        drained = dead.drain()
        assert len(drained) == 1
        assert drained[0].predicted_number == 5
        assert dead.count() == 0


class TestRoleRepository:
    def test_system_roles_are_seeded_on_first_read(self, data_dir) -> None:
        from zz2.repositories.roles import RoleRepository

        roles = RoleRepository().all()
        assert {role.key for role in roles} == {"super_admin", "admin", "tech"}

    def test_super_admin_drift_is_repaired(self, data_dir) -> None:
        """A ``roles.json`` written by an older build must not be able to strip
        permissions from the only role that could undo the change."""
        from zz2.domain.permissions import ALL_PERMISSIONS
        from zz2.paths import ROLES_FILE
        from zz2.repositories.roles import RoleRepository

        ROLES_FILE.write_text(json.dumps([
            {"key": "super_admin", "label": "Super Admin", "rank": 5,
             "permissions": ["tickets"], "system": True, "root": False},
        ]), encoding="utf-8")

        role = RoleRepository().by_key("super_admin")
        assert role.is_root is True
        assert role.rank == 100
        assert role.effective_permissions == ALL_PERMISSIONS

    def test_missing_system_role_is_reseeded(self, data_dir) -> None:
        """A deleted ``tech`` role would leave its users resolving to no
        permissions, which looks like an unexplained lockout."""
        from zz2.paths import ROLES_FILE
        from zz2.repositories.roles import RoleRepository

        ROLES_FILE.write_text(json.dumps([
            {"key": "admin", "label": "Admin", "rank": 50, "system": True,
             "permissions": ["tickets", "dashboard"]},
        ]), encoding="utf-8")

        assert RoleRepository().by_key("tech") is not None

    def test_unknown_role_resolves_to_nothing(self, data_dir) -> None:
        from zz2.repositories.roles import RoleRepository

        permissions, rank, is_root = RoleRepository().resolve("deleted_role")
        assert permissions == frozenset()
        assert rank == 0
        assert is_root is False


class TestStatsRepository:
    def test_period_rollover_resets_the_right_counters(self, data_dir) -> None:
        """No scheduler involved: a changed period label triggers the reset."""
        import datetime as dt

        from zz2.repositories.stats import StatsRepository

        stats = StatsRepository()
        monday = dt.date(2026, 6, 1)

        stats.increment("ana", today=monday)
        stats.increment("ana", today=monday)
        record = stats.all()["ana"]
        assert (record["today"], record["total"]) == (2, 2)

        # Next day: the daily counter resets, the lifetime total does not.
        record = stats.increment("ana", today=monday + dt.timedelta(days=1))
        assert record["today"] == 1
        assert record["total"] == 3

        # Two months on: every rolling counter resets.
        record = stats.increment("ana", today=dt.date(2026, 8, 20))
        assert record["today"] == 1
        assert record["this_month"] == 1
        assert record["total"] == 4


class TestDraftRepository:
    def test_drafts_are_scoped_per_user(self, data_dir) -> None:
        from zz2.repositories.drafts import DraftRepository

        drafts = DraftRepository()
        drafts.save("ana@zz2.com.br", {"client": "A"})
        drafts.save("bruno@zz2.com.br", {"client": "B"})

        assert drafts.get("ana@zz2.com.br") == {"client": "A"}
        assert drafts.get("bruno@zz2.com.br") == {"client": "B"}

    def test_a_traversal_username_cannot_escape_the_drafts_directory(self, data_dir) -> None:
        """The username comes from a JWT ``email`` claim."""
        from zz2.paths import DRAFTS_DIR
        from zz2.repositories.drafts import DraftRepository

        DraftRepository().save("../../config", {"pwned": True})

        written = list(DRAFTS_DIR.glob("*.json"))
        assert len(written) == 1
        assert written[0].parent == DRAFTS_DIR


class TestInitialCatalogSeed:
    @staticmethod
    def _bundle(path) -> None:
        path.mkdir(parents=True)
        (path / "kits.json").write_text(json.dumps({
            "KIT-ONE": {"vehicle": "Test vehicle", "notes": "seeded"},
        }), encoding="utf-8")
        (path / "troubleshooting.json").write_text(json.dumps({
            "KIT-ONE": [{
                "issue": "No audio", "parent": "", "steps": ["Check power"],
            }],
        }), encoding="utf-8")

    def test_existing_empty_volume_is_repaired_from_bundle(self, data_dir) -> None:
        from zz2.repositories.kits import KitCatalogRepository
        from zz2.services.catalog import CatalogService

        seed_dir = data_dir / "seed"
        self._bundle(seed_dir)
        catalog = KitCatalogRepository()
        # Reproduce the affected VPS: the old startup already created empty
        # files and stamped the troubleshooting seed version.
        catalog.kits.write({})
        catalog.trees.write({})
        catalog.meta.mark_seeded()

        summary = CatalogService(seed_dir=seed_dir).ensure_initialised()

        assert summary["kits"] == 1
        assert catalog.kits.get("KIT-ONE")["vehicle"] == "Test vehicle"
        assert len(catalog.trees.get("KIT-ONE")) == 1
        assert catalog.meta.bundle_seeded() is True

    def test_bundle_does_not_resurrect_intentionally_deleted_kits(self, data_dir) -> None:
        from zz2.repositories.kits import KitCatalogRepository
        from zz2.services.catalog import CatalogService

        seed_dir = data_dir / "seed"
        self._bundle(seed_dir)
        service = CatalogService(seed_dir=seed_dir)
        service.ensure_initialised()

        catalog = KitCatalogRepository()
        catalog.kits.write({})
        catalog.trees.write({})

        summary = service.ensure_initialised()

        assert summary["kits"] == 0
        assert catalog.kits.all() == {}
