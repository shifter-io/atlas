"""The report pages on an empty or freshly seeded database, and the storage schema lock."""

from datetime import UTC, datetime, timedelta

import pytest
from psycopg.rows import dict_row

from atlas import report_html, report_parts
from atlas.orchestrator.control import write_settings
from atlas.orchestrator.planparts import next_slot
from atlas.report_data import batch_data, index_data
from atlas.storage import SCHEMA_VERSION, connect
from atlas.storage_seed import seed

pytest_plugins = ["test_storage"]

NOW = datetime(2026, 10, 1, 11, 59, 30, tzinfo=UTC)


def test_index_of_an_empty_database_has_no_batches_and_an_unknown_batch_no_data(database):
    with connect(database) as conn:
        conn.row_factory = dict_row
        index = index_data(conn, NOW)
        assert index["batches"] == [] and index["counts"] == [] and index["census_next"] == []
        assert batch_data(conn, 999) is None


def test_census_interval_mode_lists_the_next_aligned_slot_per_country(database):
    seed(database)
    with connect(database) as conn:
        conn.row_factory = dict_row
        daily = index_data(conn, NOW)["census_next"]
        write_settings(conn, {"census.every_minutes": 15})
        interval = index_data(conn, NOW)["census_next"]
    slot = next_slot(timedelta(minutes=15), NOW)
    assert slot == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert interval and {row["next_at"] for row in interval} == {slot}
    # The interval mode lists the same countries, in country order rather than by time.
    assert [row["country_code"] for row in interval] == sorted(r["country_code"] for r in interval)
    assert len(interval) == len(daily)


def test_write_skips_unknown_batches_and_still_writes_the_index(database, tmp_path):
    result = report_html.write(database, tmp_path, [999], now=NOW)
    assert result["batches"] == [] and result["index"] == "index.html"
    assert not (tmp_path / "batch-999.html").exists()
    assert "<html" in (tmp_path / "index.html").read_text()


def test_write_atomic_leaves_the_old_page_and_no_temporary_file_on_failure(tmp_path, monkeypatch):
    page = tmp_path / "index.html"
    page.write_text("old")

    def fail(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(report_html.os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        report_html._write_atomic(page, "new")
    assert page.read_text() == "old" and [p.name for p in tmp_path.iterdir()] == ["index.html"]


def test_stop_reason_and_timeline_without_data():
    assert report_parts.stop_reason(None, "failed") == "Failed before recording a stop reason."
    assert report_parts.stop_reason(None, "done") == "Finished."
    assert report_parts.stop_reason("mystery", "done") == "Stopped: mystery."
    assert report_parts.timeline([]) == ""


def test_seed_refuses_a_database_with_another_schema_version(database):
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO schema_versions(version,checksum) VALUES (%s,'newer')",
            (SCHEMA_VERSION + 1,),
        )
    with pytest.raises(ValueError, match="requires db-migrate or a rebuild before seeding"):
        seed(database)
