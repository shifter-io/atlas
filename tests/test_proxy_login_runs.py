"""PROXY-AUTH-ALERT-1: a run whose requests were mostly refused at login (HTTP 407) measured
nothing. detect (step and fast replay) never judges such a pulse and leaves it out of later
baselines; a daily test refused at login is not imported. The pulse evaluator's alert is in
test_proxy_login.py."""

import json

import pytest
from calculated_pulse_cases import T0, after, pulses
from census_pulse_cases import product
from daily_cases import daily_root
from test_onboarding import START
from test_storage import measurement

from atlas.daily import ingest as daily_ingest
from atlas.daily import tape
from atlas.onboarding.files import read_json
from atlas.storage import connect, ingest, seed

pytest_plugins = ["test_storage", "test_enrichment_cli"]

JUDGED = (
    "SELECT product_id,kind FROM test_events WHERE kind IN ('down','success_drop','recovered',"
    "'pulse_finished') ORDER BY event_id"
)


async def stored_runs(tmp_path, database, refused):
    """A stored run per entry, whose summary says that many of its requests were refused."""
    keys = []
    for i, count in enumerate(refused):
        key = ingest(await measurement(tmp_path / f"run-{i}"), database)["run_key"]
        with connect(database) as conn:
            conn.execute(
                "UPDATE test_runs SET summary=jsonb_set(summary,'{errors}',"
                "jsonb_build_object('proxy_auth',%s::int)) WHERE run_key=%s",
                (count, key),
            )
        keys.append(key)
    return keys


@pytest.mark.parametrize("fast", [False, True])
async def test_pulses_refused_at_login_are_never_judged_nor_a_baseline(database, tmp_path, fast):
    """alpha gets nothing at pulses 6 and 7 on both targets, every request refused: no 'down',
    no outage, and pulse 8 is compared with pulse 5, not with a refused one. beta's drop to 40%
    has 55% refused: no success_drop; gamma's, without refusals, is one."""
    seed(database)
    keys = await stored_runs(tmp_path, database, [100, 100, 100, 100, 55])
    with connect(database) as conn:
        alpha, beta, gamma = (product(conn, name) for name in ("alpha", "beta", "gamma"))
        for target, (six, seven) in (("generic_http", keys[0:2]), ("generic_https", keys[2:4])):
            refused = {6: six, 7: seven}
            pulses(conn, alpha, [95] * 6 + [0, 0, 95], run_keys=refused, target=target)
        pulses(conn, beta, [95] * 6 + [{"successes": 40}], run_keys={6: keys[4]}, country="FR")
        pulses(conn, gamma, [95] * 6 + [{"successes": 40}], country="PL")
    with connect(database) as conn:
        tape.replay(conn, T0, after(9), fast=fast)
        assert conn.execute(JUDGED).fetchall() == [(gamma, "success_drop")]
        assert conn.execute("SELECT count(*) FROM outages").fetchone()[0] == 0


async def test_a_daily_test_refused_at_login_is_not_imported(
    tmp_path, monkeypatch, database, fake_database
):
    """tor1's first country answered 407 to every request: it is not stored (no run, no verdict,
    no estimate), its cell fails by our side, and its raw files are still kept."""
    import atlas.runner as runner

    monkeypatch.setattr(runner.time, "time", lambda: START.timestamp() + 1)
    seed(database)
    root = await daily_root(tmp_path)
    mmdb, _ = fake_database
    real, converted = daily_ingest.convert_country, []

    def convert(folder, runs):
        directory = real(folder, runs)
        converted.append(directory)
        if len(converted) == 1:
            summary = read_json(directory / "summary.json")
            summary["errors"] = {"proxy_auth": summary["completed"]}
            (directory / "summary.json").write_text(json.dumps(summary))
        return directory

    monkeypatch.setattr(daily_ingest, "convert_country", convert)
    spool = tmp_path / "spool"
    report = await daily_ingest.ingest_campaign(root, database, mmdb, spool=spool)
    tor1 = report["cells"]["shifter/tor1"]
    assert (tor1["status"], tor1["ingested"]) == ("failed", ["PM"])
    assert tor1["issues"] == ["CA: not imported (proxy login refused)"]
    with connect(database) as conn:
        daily = "SELECT count(*) FROM test_runs WHERE summary->>'test_kind'='daily'"
        assert conn.execute(daily).fetchone()[0] == 1
        verdicts = "SELECT country_code FROM country_verdicts"
        assert conn.execute(verdicts).fetchall() == [("PM",)]
    assert len(list(spool.glob("*/*/*/*/shifter/daily-*.tar.gz"))) == 2
