"""WIPE-TOOL-1: the wipe removes exactly the replay's rows, after a REPLAY-1 replay of the
fixture slice set on the test database (standing in for staging)."""

import asyncio
import importlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from orchestrator_cases import query
from storage_cases import enrichment, measurement
from test_scale_replay import argv, staging  # noqa: F401 (the fixture)

from proxy_index.partitions import DAILY, children
from proxy_index.storage import ingest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scale"))
replay = importlib.import_module("replay")
replay_baseline = importlib.import_module("replay_baseline")
wipe_replay = importlib.import_module("wipe_replay")

pytest_plugins = ["test_storage"]

GO = f"owner-go LAUNCH-WIPE {datetime.now(UTC).date()}"
PROTECTED = ("settings", "countries", "runner_regions", "methodologies", "targets")
PROTECTED += ("schema_versions", "calculation_schema")


def counts(database):
    with replay.storage.connect(database) as conn:
        return replay_baseline.counts(conn)


def rows(database):
    """Every row of the protected tables."""
    return {name: sorted(map(repr, query(database, f"SELECT * FROM {name}"))) for name in PROTECTED}


def baseline_rows(database):
    """Production's own rows besides the seed, which the wipe must keep."""
    with replay.storage.connect(database) as conn:
        conn.execute("INSERT INTO providers(id) VALUES ('baseline')")
        conn.execute("INSERT INTO products(provider,name) VALUES ('baseline','res')")
        conn.execute("INSERT INTO ip_addresses(ip) VALUES ('198.51.100.7')")
        conn.execute("INSERT INTO place_names(country_code,city) VALUES ('US','Baseline')")


def published(conn):
    """A published build with a snapshot (a stand-in: no windows)."""
    build = conn.execute(
        "INSERT INTO calculation_builds(methodology_id,as_of,input_hash,published) "
        "VALUES ('baseline',now(),gen_random_uuid()::text,true) RETURNING build_id"
    ).fetchone()[0]
    conn.execute("INSERT INTO calculated_snapshots VALUES (%s,'baseline','{}')", (build,))
    return build


def wipe(database, tmp_path, capsys, *extra):
    state = tmp_path / "out" / "state.json"
    code = wipe_replay.main(["--database", str(database), "--state", str(state), *extra])
    out, err = capsys.readouterr()
    return code, json.loads(out) if code == 0 else err


def growth(before, after):
    return {name: after[name] - before[name] for name in after if after[name] != before[name]}


def replay_rows(base, replayed):
    """The rows the replay added outside the protected tables, per table."""
    found = growth(base, replayed)
    return {name: n for name, n in found.items() if name.split(".")[1] not in PROTECTED}


def left(base, replayed, own=None):
    """The counts after the wipe: the baseline's, the protected tables as the replay left them,
    plus production's own rows."""
    own = own or {}
    kept = {name: replayed[name] for name in replayed if name.split(".")[1] in PROTECTED}
    return {name: kept.get(name, base[name]) + own.get(name, 0) for name in base}


def test_the_dry_run_counts_and_the_apply_leaves_exactly_the_baseline(
    staging,  # noqa: F811
    tmp_path,
    capsys,
):
    baseline_rows(staging)
    base, protected = counts(staging), rows(staging)
    assert replay.main(argv(staging, tmp_path, 1, 2)) == 0
    capsys.readouterr()
    state = json.loads((tmp_path / "out" / "state.json").read_text())
    replayed, replayed_rows = counts(staging), rows(staging)
    code, refused = wipe(staging, tmp_path, capsys, "--apply", "--owner-go", GO)
    assert code == 2 and "no dry run" in refused
    code, dry = wipe(staging, tmp_path, capsys)
    assert code == 0 and dry["dry_run"] and counts(staging) == replayed
    assert dry["deleted"] == replay_rows(base, replayed)
    assert dry["deleted"]["public.test_runs"] == replayed["public.test_runs"] > 4
    assert dry["deleted"]["public.nodes"] == len(state["nodes"])
    assert dry["deleted"]["public.calculation_builds"] >= 1
    assert dry["empty_partitions"] and dry["current_build"] is None
    added = sorted(set(map(eval, replayed_rows["targets"])) - set(map(eval, protected["targets"])))
    assert dry["targets_kept"] == [row[0] for row in added]
    for extra in ([], ["--owner-go", "owner-go LAUNCH-WIPE"], ["--owner-go", "owner-go X"]):
        code, refused = wipe(staging, tmp_path, capsys, "--apply", *extra)
        assert code == 2 and "LAUNCH-WIPE" in refused
    later = f"owner-go LAUNCH-WIPE {datetime.now(UTC).date().replace(year=2999)}"
    code, refused = wipe(staging, tmp_path, capsys, "--apply", "--owner-go", later)
    assert code == 2 and "after today" in refused
    assert counts(staging) == replayed
    code, applied = wipe(staging, tmp_path, capsys, "--apply", "--owner-go", GO)
    assert code == 0 and not applied["dry_run"] and applied["deleted"] == dry["deleted"]
    assert counts(staging) == left(base, replayed) == applied["counts"]
    assert set(applied["not_at_baseline"]) == set(growth(base, left(base, replayed)))
    assert rows(staging) == replayed_rows  # the protected tables, row by row
    for name, found in protected.items():
        assert set(found) <= set(replayed_rows[name])
    assert query(staging, "SELECT count(*) FROM current_calculation") == [(0,)]
    with replay.storage.connect(staging) as conn:
        partitions = [name for table in DAILY for name, _, _ in children(conn, table)]
    assert not set(dry["empty_partitions"]) & set(partitions)


def test_a_production_run_after_the_baseline_survives(staging, tmp_path, capsys):  # noqa: F811
    base = counts(staging)
    assert replay.main(argv(staging, tmp_path, 1, 1)) == 0
    capsys.readouterr()
    replayed = counts(staging)
    for provider in ("fixture", "other"):  # production's own runs, without the marker
        (tmp_path / provider).mkdir()
        directory = asyncio.run(measurement(tmp_path / provider, provider=provider))
        enrichment(directory)
        assert ingest(directory, staging)["imported"]
    overlap = "SELECT provider_a,provider_b,shared_ips FROM provider_overlap"
    assert query(staging, overlap) == [("fixture", "other", 1)]
    own = growth(replayed, counts(staging))
    with replay.storage.connect(staging) as conn:  # as if the replay shared their IPs
        conn.execute("UPDATE provider_overlap SET shared_ips=shared_ips+4")
    code, dry = wipe(staging, tmp_path, capsys)
    assert code == 0 and dry["deleted"] == replay_rows(base, replayed)
    # The counts the dry run kept differ from the apply's: it rolls back.
    path = tmp_path / "out" / wipe_replay.DRY_RUN
    tampered = json.loads(path.read_text())
    tampered["deleted"]["public.test_runs"] += 1
    path.write_text(json.dumps(tampered))
    code, refused = wipe(staging, tmp_path, capsys, "--apply", "--owner-go", GO)
    assert code == 2 and "rolled back" in refused
    assert counts(staging) == {name: replayed[name] + own.get(name, 0) for name in replayed}
    assert wipe(staging, tmp_path, capsys)[0] == 0
    code, applied = wipe(staging, tmp_path, capsys, "--apply", "--owner-go", GO)
    assert code == 0 and applied["counts"] == left(base, replayed, own)
    marked = "SELECT count(*) FILTER (WHERE summary ? 'scale_replay'),count(*) FROM test_runs"
    assert query(staging, marked) == [(0, 2)]
    assert query(staging, "SELECT count(*) FROM products WHERE provider='fixture'")[0][0] == 1
    assert query(staging, "SELECT count(*) FROM ip_observations")[0][0] > 0
    assert query(staging, overlap) == [("fixture", "other", 1)]


def test_a_current_replay_build_gives_way_to_the_newest_baseline_build(staging):  # noqa: F811
    with replay.storage.connect(staging) as conn:
        conn.execute("INSERT INTO methodologies VALUES ('baseline','{}')")
        older, kept, replayed = published(conn), published(conn), published(conn)
        conn.execute("INSERT INTO current_calculation(build_id) VALUES (%s)", (replayed,))
        assert wipe_replay.builds(conn, kept) == kept
        found = conn.execute("SELECT build_id FROM calculation_builds ORDER BY 1").fetchall()
        assert found == [(older,), (kept,)]
        conn.execute("UPDATE current_calculation SET build_id=%s", (older,))
        assert wipe_replay.builds(conn, kept) == older  # a baseline build stays current


def test_a_state_of_another_database_is_refused(staging, tmp_path, capsys):  # noqa: F811
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "state.json").write_text(json.dumps({"database": "elsewhere"}))
    code, refused = wipe(staging, tmp_path, capsys)
    assert code == 2 and "not of" in refused
