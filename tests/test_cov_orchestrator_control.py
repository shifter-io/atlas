"""Operator switches from the command line (orchestrator/control.py): the defaults of an unset
setting, switching one planner, and the refused actions, kinds and settings."""

import pytest
from psycopg.rows import dict_row

from proxy_index.orchestrator.control import read_setting, scheduler, state, write_settings
from proxy_index.orchestrator.db import DEFAULTS
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]


def settings(config):
    with connect(config) as conn:
        return dict(conn.execute("SELECT key,value FROM settings ORDER BY key").fetchall())


def test_status_reads_the_defaults_and_writes_nothing(database):
    before = settings(database)
    assert scheduler(str(database), "status") == {
        "census": DEFAULTS["scheduler.census_enabled"],
        "pulse": DEFAULTS["scheduler.pulse_enabled"],
        "census_every_minutes": DEFAULTS["census.every_minutes"],
        "reports_dir": DEFAULTS["reports.dir"],
    }
    assert settings(database) == before


def test_off_switches_only_the_given_planner_and_on_switches_it_back(database):
    off = scheduler(str(database), "off", ["pulse"])
    assert (off["census"], off["pulse"]) == (True, False)
    assert settings(database)["scheduler.pulse_enabled"] is False
    assert settings(database).get("scheduler.census_enabled", True) is True
    both = scheduler(str(database), "off")
    assert (both["census"], both["pulse"]) == (False, False)
    back = scheduler(str(database), "on", ["census"])
    assert (back["census"], back["pulse"]) == (True, False)


def test_read_setting_returns_the_value_on_a_dict_row_connection(database):
    with connect(database) as conn:
        write_settings(conn, {"census.every_minutes": 15})
        conn.row_factory = dict_row
        assert read_setting(conn, "census.every_minutes") == 15
        assert read_setting(conn, "reports.dir") == DEFAULTS["reports.dir"]
        assert state(conn)["census_every_minutes"] == 15


def test_write_settings_refuses_unknown_keys_and_writes_none_of_them(database):
    with connect(database) as conn:
        with pytest.raises(ValueError, match=r"Unknown settings: bogus\.a, bogus\.b"):
            write_settings(conn, {"census.every_minutes": 5, "bogus.b": 1, "bogus.a": 2})
    assert "census.every_minutes" not in settings(database)


@pytest.mark.parametrize(
    ("action", "kinds", "message"),
    [
        ("toggle", None, "action must be on, off or status"),
        ("on", ["census", "calculate"], "kinds must be census and/or pulse"),
    ],
)
def test_scheduler_refuses_bad_actions_and_kinds_before_connecting(
    tmp_path, action, kinds, message
):
    # The file does not exist: a refusal must come before any connection attempt.
    with pytest.raises(ValueError, match=message):
        scheduler(str(tmp_path / "missing.toml"), action, kinds)
