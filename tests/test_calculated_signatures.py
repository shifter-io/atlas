"""List signature v1 (CALC-LISTS-1b): a daily group's window whose inputs are unchanged reuses its
stored list, a new test gives a new list, and verify-on-hit fails a build whose signature missed
an input, so the previous build keeps serving."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from calculated_cases import POLICY, payload_mismatches, spread
from test_storage import enrichment, measurement

from proxy_index.calculated import calculator, lists, store
from proxy_index.calculated.calculator import calculate
from proxy_index.calculated.policy import digest
from proxy_index.storage import connect, ingest

pytest_plugins = ["test_storage", "calculated_template"]


@pytest.fixture
def database(calculated_database):
    """Every test here works on the current calculated schema, copied from its template."""
    return calculated_database


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


def signed(conn, build, hours, spans):
    """Signature v1 of a build's target-free window of `hours` (expected, by hand): spans maps each
    unenriched run_key to its first and last bucket in the window."""
    countries = conn.execute("SELECT country_code,name FROM countries").fetchall()
    runs = []
    for run_key, (first, last) in sorted(spans.items()):
        (summary,) = conn.execute(
            "SELECT summary_sha256 FROM test_runs WHERE run_key=%s", (run_key,)
        ).fetchone()
        runs.append([run_key, summary, None, None, first.isoformat(), last.isoformat(), True])
    return digest(
        {
            "v": 1,
            "impl": calculator.IMPLEMENTATION_ID,
            "method": build["methodology"]["id"],
            "hours": hours,
            "countries": digest(sorted([list(c) for c in countries])),
            "runs": runs,
        }
    )


def by_country(build):
    """Requested country -> cohort of the build's product groups."""
    return {p["criteria"]["country"]: p["cohort"] for p in build["products"]}


@pytest.mark.asyncio
async def test_identical_consecutive_builds_write_no_new_list_rows(database, tmp_path):
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
    signatures = lists.signatures

    def without_runs(conn, payload, runs, buckets):
        """A deliberately incomplete signature: it leaves the runs out."""
        return {key: digest(list(key)) for key in signatures(conn, payload, runs, buckets)}

    monkeypatch.setattr(lists, "signatures", without_runs)
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


@pytest.mark.asyncio
async def test_a_window_edge_through_a_run_signs_its_clipped_buckets(
    database, tmp_path, monkeypatch
):
    """A run split by window edges (CALC-LISTS-1b review m1): its buckets at W, +5, +10, +30 and
    +35 minutes. Each window signs only the buckets inside it, so a cutoff that clips the run
    gives a new list (a miss) and a later cutoff with the same clipped first and last bucket
    reuses it (a hit, verified)."""
    w = datetime(2026, 10, 1, 12, tzinfo=UTC)
    minute = timedelta(minutes=1)
    ingest(await spread(tmp_path, monkeypatch, w + minute, [0, 300, 600, 1800, 2100]), database)
    with connect(database) as conn:
        (run_key,) = conn.execute("SELECT run_key FROM test_runs").fetchone()
    day, at = timedelta(hours=24), lambda m: w + m * minute
    # (cutoff, 24h span and completed, 7d/30d span): a late run (it ends after the first cutoff),
    # the whole run, then the 24h window's start moving through it.
    cases = [
        (at(20), (at(0), at(10)), 3, (at(0), at(10))),
        (at(40), (at(0), at(35)), 5, (at(0), at(35))),
        (day + at(15), (at(30), at(35)), 2, (at(0), at(35))),
        (day + at(25), (at(30), at(35)), 2, (at(0), at(35))),
        (day + at(35), (at(35), at(35)), 1, (at(0), at(35))),
    ]
    found = []
    for as_of, short, completed, long in cases:
        build = calculate(database, POLICY, as_of=as_of, publish=True)
        windows = build["products"][0]["windows"]
        assert windows["24h"]["metrics"]["completed"] == completed
        assert (
            windows["7d"]["metrics"]["completed"]
            == windows["30d"]["metrics"]["completed"]
            == (5 if long[1] == at(35) else 3)
        )
        with connect(database) as conn:
            ids = {k[2]: v for k, v in lists_of(conn, build).items()}
            stored = dict(
                conn.execute(
                    "SELECT l.window_hours,l.signature FROM calculated_lists l "
                    "WHERE l.list_id=ANY(%s)",
                    (list(ids.values()),),
                ).fetchall()
            )
            for hours, span in ((24, short), (168, long), (720, long)):
                assert stored[hours] == signed(conn, build, hours, {run_key: span}), (as_of, hours)
        assert payload_mismatches(database, int(build["snapshot_id"])) == []
        found.append(ids)
    late, whole, clipped, same, last = found
    assert set(whole.values()).isdisjoint(late.values())  # the late run's tail arrived: misses
    # The 24h window clips the run: a miss; 7d and 30d still hold the whole run: hits.
    assert clipped[24] not in (whole[24], late[24])
    assert clipped[168] == whole[168] and clipped[720] == whole[720]
    # Its start moves on, but the clipped first and last bucket stay +30 and +35: a hit.
    assert same == clipped
    # The start passes +30: a miss for 24h only.
    assert last[24] != same[24] and (last[168], last[720]) == (whole[168], whole[720])
