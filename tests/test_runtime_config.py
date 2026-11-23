"""Runtime configuration: clamping on read, validation on write.

The monolith expressed the same rules twice — as ``_cfg_num`` clamps when
reading, and again as ``setnum`` validation when writing. The two lists drifted:
``auto_resolve_days`` was writable in the range 1–365 but had no read-side clamp
at all, so a bad value stored by an older build was used verbatim.

One declaration, two behaviours: reads clamp (never fail, so a bad stored value
cannot stop the server booting), writes reject (so an operator is told rather
than silently corrected).
"""

from __future__ import annotations

import datetime as dt

import pytest

from zz2.core.errors import ValidationError


@pytest.fixture
def config(data_dir):
    from zz2.services.runtime_config import RuntimeConfigService

    return RuntimeConfigService()


class TestReading:
    def test_missing_keys_fall_back_to_defaults(self, config) -> None:
        assert config.poll_interval == 30
        assert config.reconcile_days == 2
        assert config.backup_include_media is False

    def test_out_of_range_stored_values_are_clamped_not_fatal(self, config, data_dir) -> None:
        """A value written by an older build must not stop the server starting."""
        from zz2.core.jsonstore import write_json
        from zz2.paths import CONFIG_FILE

        write_json(CONFIG_FILE, {"poll_interval": 0, "reconcile_days": 9999,
                                 "auto_resolve_days": -5})

        assert config.poll_interval == 5      # clamped up to the minimum
        assert config.reconcile_days == 30    # clamped down to the maximum
        assert config.auto_resolve_days == 1  # the gap the monolith had

    def test_unparseable_values_fall_back(self, config, data_dir) -> None:
        from zz2.core.jsonstore import write_json
        from zz2.paths import CONFIG_FILE

        write_json(CONFIG_FILE, {"poll_interval": "not a number",
                                 "work_start": "99:99"})
        assert config.poll_interval == 30
        assert config.get("work_start") == "09:30"

    def test_unknown_keys_raise_rather_than_defaulting(self, config) -> None:
        """A typo becomes an error here, not a silently-never-applied setting."""
        with pytest.raises(KeyError):
            config.get("pol_interval")

    def test_an_out_of_band_edit_is_picked_up(self, config, data_dir) -> None:
        """Reads are cached on mtime, so an operator with an editor — or a
        restore from backup — takes effect without a restart."""
        from zz2.core.jsonstore import write_json
        from zz2.paths import CONFIG_FILE

        assert config.poll_interval == 30
        write_json(CONFIG_FILE, {"poll_interval": 90})
        assert config.poll_interval == 90


class TestWriting:
    def test_valid_changes_are_persisted_and_reported(self, config) -> None:
        changed = config.apply({"poll_interval": 60, "reconcile_days": 5},
                               actor="root@zz2.com.br")
        assert set(changed) == {"poll_interval", "reconcile_days"}
        assert config.poll_interval == 60

    def test_unchanged_values_report_no_change(self, config) -> None:
        config.apply({"poll_interval": 60}, actor="root@zz2.com.br")
        assert config.apply({"poll_interval": 60}, actor="root@zz2.com.br") == []

    def test_out_of_range_writes_are_rejected_not_clamped(self, config) -> None:
        """The operator must be told, not silently corrected."""
        with pytest.raises(ValidationError, match="between"):
            config.apply({"poll_interval": 0}, actor="root@zz2.com.br")
        assert config.poll_interval == 30, "nothing should have been written"

    def test_a_partial_failure_writes_nothing(self, config) -> None:
        """Whole-payload validation: one bad value cannot leave half applied."""
        with pytest.raises(ValidationError):
            config.apply({"poll_interval": 60, "reconcile_days": 9999},
                         actor="root@zz2.com.br")
        assert config.poll_interval == 30

    def test_unknown_keys_are_rejected(self, config) -> None:
        with pytest.raises(ValidationError, match="Unknown settings"):
            config.apply({"nonsense": 1}, actor="root@zz2.com.br")

    @pytest.mark.parametrize("key", [
        "secret_key", "database_url", "session_secret",
        "azure_storage_connection_string", "microsoft_client_secret",
        "supabase_service_key", "supabase_jwt_secret",
        "supabase_publishable_key", "supabase_url",
    ])
    def test_secrets_cannot_be_written_through_the_api(self, config, key: str) -> None:
        """A browser-writable secret is a browser-readable secret one bug later."""
        with pytest.raises(ValidationError, match="server environment"):
            config.apply({key: "anything"}, actor="root@zz2.com.br")

    def test_hhmm_values_are_normalised(self, config) -> None:
        config.apply({"work_start": "9:5"}, actor="root@zz2.com.br")
        assert config.get("work_start") == "09:05"

    def test_invalid_hhmm_is_rejected(self, config) -> None:
        for bad in ("25:00", "12:70", "noon", "12", ""):
            with pytest.raises(ValidationError, match="HH:MM"):
                config.apply({"work_end": bad}, actor="root@zz2.com.br")

    def test_appscript_url_must_be_an_apps_script_deployment(self, config) -> None:
        """The URL is fetched server-side with the shared secret attached, so an
        unconstrained value is a request-forgery primitive."""
        for bad in ("http://169.254.169.254/latest/meta-data/",
                    "https://evil.example/exec",
                    "http://script.google.com/macros/s/x/exec",
                    "https://script.google.com/macros/s/x/dev"):
            with pytest.raises(ValidationError):
                config.apply({"appscript_url": bad}, actor="root@zz2.com.br")

    def test_a_valid_appscript_url_is_accepted(self, config) -> None:
        good = "https://script.google.com/macros/s/AKfy123/exec"
        assert config.apply({"appscript_url": good}, actor="root@zz2.com.br") == [
            "appscript_url"
        ]

    def test_the_previous_config_is_snapshotted(self, config, data_dir) -> None:
        """So a bad settings change is recoverable."""
        from zz2.paths import BACKUPS_DIR

        config.apply({"poll_interval": 60}, actor="root@zz2.com.br")
        config.apply({"poll_interval": 90}, actor="root@zz2.com.br")

        snapshots = list((BACKUPS_DIR / "config").glob("config_*.json"))
        assert snapshots


class TestWorkWindow:
    def test_disabled_means_always_open(self, config) -> None:
        assert config.within_work_window(dt.datetime(2026, 6, 1, 3, 0)) is True

    def test_enabled_window_gates_by_hour(self, config) -> None:
        config.apply({"work_window_enabled": True, "work_start": "09:30",
                      "work_end": "20:00"}, actor="root@zz2.com.br")

        assert config.within_work_window(dt.datetime(2026, 6, 1, 9, 0)) is False
        assert config.within_work_window(dt.datetime(2026, 6, 1, 12, 0)) is True
        assert config.within_work_window(dt.datetime(2026, 6, 1, 20, 0)) is False

    def test_a_window_wrapping_past_midnight_is_handled(self, config) -> None:
        config.apply({"work_window_enabled": True, "work_start": "22:00",
                      "work_end": "06:00"}, actor="root@zz2.com.br")

        assert config.within_work_window(dt.datetime(2026, 6, 1, 23, 0)) is True
        assert config.within_work_window(dt.datetime(2026, 6, 1, 3, 0)) is True
        assert config.within_work_window(dt.datetime(2026, 6, 1, 12, 0)) is False
