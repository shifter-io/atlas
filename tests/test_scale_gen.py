"""SCALE-GEN-1a: the synthetic data generator, on a 2 network x 3 country x 2 day miniature."""

import importlib
import ipaddress
import sys
from argparse import ArgumentTypeError, Namespace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from proxy_index.calculated import store
from proxy_index.calculated.calculator import calculate
from proxy_index.providers.credentials import read_credentials
from proxy_index.storage import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scale"))
generate = importlib.import_module("generate")
synthetic = importlib.import_module("synthetic")

pytest_plugins = ["test_storage"]

POLICY = Path("configs/methodology.toml")
END = "2026-10-03"
# AQ has no formula entry anywhere, so it takes the network curve.
MINIATURE = {"networks": "iproyal,decodo", "countries": "AQ,DE,US", "total_days": 2}
TABLES = (
    "ip_addresses",
    "ip_enrichment",
    "ip_observations",
    "product_ips",
    "pool_ips",
    "pool_stats",
    "scope_stats",
    "test_runs",
    "run_buckets",
    "run_geo_buckets",
    "run_enrichments",
    "test_batches",
    "batch_runs",
    "estimated_hours",
    "dbip_releases",
    "providers",
    "products",
    "place_names",
    "enrichment_locations",
    "asn_names",
    "enrichment_networks",
)


def options(config, days, **changes):
    values = MINIATURE | {
        "database": config,
        "days": generate.parse_days(days),
        "seed": 7,
        "end_date": END,
        "scale": 0.004,
        "formulas": str(generate.FORMULAS),
        "jobs": 1,
    }
    return Namespace(**(values | changes))


def run(config, days, **changes):
    name = read_credentials(config, "postgres")["dbname"]
    return generate.generate(options(config, days, **changes), name)


def model(config, **changes):
    args = options(config, "1", **changes)
    formulas, names = generate.load(args.formulas)
    return synthetic.Model(generate.requested(args, formulas, names), formulas, names)


def fingerprint(config):
    with connect(config) as conn:
        return {
            table: conn.execute(
                f"SELECT count(*),md5(coalesce(string_agg(t::text,'|' ORDER BY t::text),'')) "
                f"FROM {table} t"
            ).fetchone()
            for table in TABLES
        }


def test_the_miniature_has_the_rows_the_plan_counts(databases):
    config = databases()
    result = run(config, "1..2")
    assert result["generated"] == [1, 2] and result["days_done"] == 2
    expected = synthetic.plan(model(config), [1, 2])
    found = {table: rows for table, (rows, _) in fingerprint(config).items()}
    assert found == {table: expected[table] for table in TABLES}
    with connect(config) as conn:
        # Only synthetic address space; IPv4 and IPv6 both used.
        families = conn.execute(
            "SELECT family(ip),count(*) FILTER (WHERE NOT (ip << %s OR ip << '2001:db8::/32')) "
            "FROM ip_addresses GROUP BY 1 ORDER BY 1",
            (str(ipaddress.ip_network((0xF0000000, 4))),),
        ).fetchall()
        assert families == [(4, 0), (6, 0)]
        # Day 2 reuses 30-60% of the day before's IPs; some IPs are in two countries.
        reused = conn.execute(
            "SELECT avg((o.ip_id IN (SELECT ip_id FROM ip_observations WHERE bucket_start<%s))"
            "::int) FROM (SELECT DISTINCT ip_id FROM ip_observations WHERE bucket_start>=%s) o",
            (datetime(2026, 10, 2, tzinfo=UTC),) * 2,
        ).fetchone()[0]
        assert 0.3 <= reused <= 0.6
        shared = conn.execute(
            "SELECT count(*) FROM (SELECT ip_id FROM pool_ips GROUP BY 1 HAVING count(*)>1) s"
        ).fetchone()[0]
        assert shared > 0
        # Hours that ended by the last day's end are released, later ones are not yet.
        open_hours = conn.execute(
            "SELECT bool_and(day+make_interval(hours=>hour+1)>%s),count(*) FROM estimated_hours "
            "WHERE released_at IS NULL",
            (datetime(2026, 10, 3, tzinfo=UTC),),
        ).fetchone()
        assert open_hours[0] and open_hours[1] > 0
        # Real ingests continue after the explicit ids.
        last = model(config).run_key(2, 6 * 25 - 1)
        assert conn.execute("SELECT nextval('test_runs_run_key_seq')").fetchone()[0] == last + 1


def test_steps_and_parallel_networks_give_the_same_rows_and_a_seed_its_own(databases):
    whole, steps, other = databases(), databases(), databases()
    run(whole, "1..2")
    assert run(steps, "1", jobs=2)["generated"] == [1]
    assert run(steps, "1..2", jobs=2)["generated"] == [2]
    assert run(steps, "2")["generated"] == []
    assert fingerprint(whole) == fingerprint(steps)
    run(other, "1..2", seed=8)
    assert fingerprint(other)["ip_observations"] != fingerprint(whole)["ip_observations"]


def test_an_interrupted_day_resumes_with_its_remaining_networks(databases, monkeypatch):
    whole, broken = databases(), databases()
    run(whole, "1")
    real, done = generate.part, []

    def lost_after_one(*item):
        if done:
            raise RuntimeError("worker lost")
        done.append(real(*item))

    monkeypatch.setattr(generate, "part", lost_after_one)
    with pytest.raises(RuntimeError, match="worker lost"):
        run(broken, "1")
    with connect(broken) as conn:
        progress = conn.execute("SELECT value FROM settings WHERE key='scale.generator'").fetchone()
    assert {k: progress[0][k] for k in ("days_done", "prepared", "parts")} == {
        "days_done": 0,
        "prepared": 1,
        "parts": [0],
    }
    monkeypatch.setattr(generate, "part", real)
    assert run(broken, "1")["generated"] == [1]
    assert fingerprint(broken) == fingerprint(whole)


def test_the_calculator_reads_the_miniature(databases):
    config = databases()
    run(config, "1..2")
    store.initialize(config)
    payload = calculate(config, POLICY, as_of=datetime(2026, 10, 3, tzinfo=UTC))
    assert len(payload["runs"]) == 12  # the daily tests; pulses are summarized elsewhere


def test_guards_refuse_other_databases_runs_and_settings(databases, capsys):
    fresh = databases()
    with pytest.raises(generate.Refused, match="writes only to 'proxy_scale'"):
        generate.generate(options(fresh, "1"))
    assert generate.main(["--database", str(fresh), "--days", "1"]) == 2
    assert "refused: the database is" in capsys.readouterr().err
    with pytest.raises(generate.Refused, match="day 1 must be generated before day 2"):
        run(fresh, "2")
    with connect(fresh) as conn:
        assert conn.execute("SELECT to_regclass('test_runs')").fetchone()[0] is None
    run(fresh, "1")
    with pytest.raises(generate.Refused, match="other settings: seed, scale"):
        run(fresh, "2", seed=8, scale=0.5)
    with pytest.raises(generate.Refused, match="day 3 is past the 2 days"):
        run(fresh, "2..3")
    with connect(fresh) as conn:
        conn.execute("UPDATE test_runs SET summary=summary-'scale_generator' WHERE run_key=1")
    with pytest.raises(generate.Refused, match="1 test runs this generator did not write"):
        run(fresh, "2")


def test_unknown_countries_and_another_storage_schema_are_refused(databases):
    with pytest.raises(generate.Refused, match="unknown: XX"):
        run(databases(), "1", countries="DE,XX")
    config = databases()
    run(config, "1")
    with connect(config) as conn:
        conn.execute("UPDATE schema_versions SET checksum='other'")
    with pytest.raises(generate.Refused, match="Schema differs"):
        run(config, "2")


def test_day_ranges_and_choices():
    assert generate.parse_days("3") == (3, 3) and generate.parse_days("2..31") == (2, 31)
    with pytest.raises(ArgumentTypeError):
        generate.parse_days("4..2")
    assert generate.pick(None, ["a", "b"]) == ("a", "b")
    assert generate.pick("1", ["a", "b"]) == ("a",)
    assert generate.pick("b", ["a", "b"]) == ("b",)
