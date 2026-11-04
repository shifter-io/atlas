"""List signature v1 (CALC-LISTS-1b): a daily group's window whose inputs are unchanged reuses its
stored list, a new test gives a new list, and verify-on-hit fails a build whose signature missed
an input, so the previous build keeps serving."""

import json
from datetime import datetime, timedelta

import pytest
from test_calculated import POLICY, payload_mismatches
from test_storage import enrichment, measurement

from proxy_index.calculated import calculator, store
from proxy_index.calculated.calculator import calculate
from proxy_index.calculated.policy import digest
from proxy_index.storage import connect, ingest

pytest_plugins = ["test_storage"]

LISTS = (
    "SELECT w.product_id,w.cohort,w.window_hours,w.list_id FROM calculated_windows w "
    "WHERE w.build_id=%s AND w.target='' ORDER BY 1,2,3"
)
COUNTS = (
    "SELECT (SELECT count(*) FROM calculated_lists),(SELECT count(*) FROM calculated_list_rows)"
)


async def runs(tmp_path, *countries):
    """One measured run directory per requested country, and a cutoff after all of them."""
    directories = [await measurement(tmp_path, requests=10, requested_country=c) for c in countries]
    finished = max(
        datetime.fromisoformat(json.loads((d / "summary.json").read_text())["finished_at"])
        for d in directories
    )
    return directories, finished + timedelta(minutes=5)


def lists_of(conn, build):
    """(product_id, cohort, hours) -> list_id of a build's target-free windows."""
    return {tuple(r[:3]): r[3] for r in conn.execute(LISTS, (int(build["snapshot_id"]),))}


def by_country(build):
    """Requested country -> cohort of the build's product groups."""
    return {p["criteria"]["country"]: p["cohort"] for p in build["products"]}


@pytest.mark.asyncio
async def test_identical_consecutive_builds_write_no_new_list_rows(database, tmp_path):
    store.initialize(database)
    (us,), end = await runs(tmp_path, "us")
    enrichment(us)
    ingest(us, database)
    first = calculate(database, POLICY, as_of=end, publish=True)
    with connect(database) as conn:
        written, before = conn.execute(COUNTS).fetchone(), lists_of(conn, first)
    # The next cutoff sees the same runs and buckets in every window: a new build, same lists
    # (they survive retention dropping the first build).
    second = calculate(database, POLICY, as_of=end + timedelta(minutes=5), publish=True)
    assert second["snapshot_id"] != first["snapshot_id"]
    with connect(database) as conn:
        assert conn.execute(COUNTS).fetchone() == written
        assert lists_of(conn, second) == before and len(before) == 3
    assert payload_mismatches(database, int(second["snapshot_id"])) == []


@pytest.mark.asyncio
async def test_unchanged_country_shares_its_lists_and_a_new_test_gets_new_ones(database, tmp_path):
    store.initialize(database)
    (us, de, again), end = await runs(tmp_path, "us", "de", "us")
    enrichment(us)
    ingest(us, database)
    first = calculate(database, POLICY, as_of=end, publish=True)
    with connect(database) as conn:
        before = lists_of(conn, first)
    ingest(de, database)
    second = calculate(database, POLICY, as_of=end, publish=True)  # new inputs: a new build
    cohorts = by_country(second)
    with connect(database) as conn:
        after = lists_of(conn, second)
        assert len(after) == 6 and len(before) == 3
        # The unchanged US group keeps its lists; the new German group gets new ones.
        assert {k: v for k, v in after.items() if k[1] == cohorts["us"]} == before
        assert not {v for k, v in after.items() if k[1] == cohorts["de"]} & set(before.values())
        signatures = conn.execute(
            "SELECT signature FROM calculated_lists WHERE list_id=ANY(%s)", (list(after.values()),)
        ).fetchall()
        assert len({s for (s,) in signatures}) == 6 and None not in {s for (s,) in signatures}
    ingest(again, database)  # a new US test: new US lists, the German ones are reused
    third = calculate(database, POLICY, as_of=end, publish=True)
    with connect(database) as conn:
        latest = lists_of(conn, third)
    for key, list_id in latest.items():
        assert (list_id == after[key]) == (key[1] == cohorts["de"])
    assert payload_mismatches(database, int(third["snapshot_id"])) == []


@pytest.mark.asyncio
async def test_signature_v1_is_the_digest_of_its_frozen_inputs(database, tmp_path):
    store.initialize(database)
    (us,), end = await runs(tmp_path, "us")
    enrichment(us, release="b" * 64)
    ingest(us, database)
    build = calculate(database, POLICY, as_of=end)
    period = build["periods"]["24h"]
    with connect(database) as conn:
        (run_key, summary), *others = conn.execute(
            "SELECT run_key,summary_sha256 FROM test_runs"
        ).fetchall()
        release, artifact = conn.execute(
            "SELECT release_id,artifact_sha256 FROM run_enrichments WHERE run_key=%s", (run_key,)
        ).fetchone()
        first, last = conn.execute(
            "SELECT min(bucket_start),max(bucket_start) FROM run_buckets "
            "WHERE run_key=%s AND bucket_start>=%s AND bucket_start<%s",
            (run_key, period["start"], period["end"]),
        ).fetchone()
        countries = conn.execute("SELECT country_code,name FROM countries").fetchall()
        stored = conn.execute(
            "SELECT signature FROM calculated_lists WHERE window_hours=24"
        ).fetchone()[0]
    assert others == [] and first is not None
    expected = {
        "v": 1,
        "impl": calculator.IMPLEMENTATION_ID,
        "method": build["methodology"]["id"],
        "hours": 24,
        "countries": digest(sorted([list(c) for c in countries])),
        "runs": [[run_key, summary, release, artifact, first.isoformat(), last.isoformat(), True]],
    }
    assert stored == digest(expected)


@pytest.mark.asyncio
async def test_verify_on_hit_fails_an_incomplete_signature(database, tmp_path, monkeypatch):
    signatures = calculator.signatures

    def without_runs(conn, payload, runs, buckets):
        """A deliberately incomplete signature: it leaves the runs out."""
        return {key: digest(list(key)) for key in signatures(conn, payload, runs, buckets)}

    monkeypatch.setattr(calculator, "signatures", without_runs)
    store.initialize(database)
    (us, again), end = await runs(tmp_path, "us", "us")
    ingest(us, database)
    first = calculate(database, POLICY, as_of=end, publish=True)
    ingest(again, database)  # changes the US lists, not their incomplete signatures
    with pytest.raises(ValueError, match="list signature is incomplete"):
        calculate(database, POLICY, as_of=end, publish=True)
    # The failed calculation left nothing: the previous build keeps serving.
    assert store.read(database)["snapshot_id"] == first["snapshot_id"]
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM calculation_builds").fetchone()[0] == 1
    monkeypatch.undo()
    fixed = calculate(database, POLICY, as_of=end, publish=True)
    assert fixed["products"][0]["windows"]["24h"]["metrics"]["completed"] == 20
