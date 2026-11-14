"""Browser targets in the calculated snapshot (BP-CALC, browser pulse K-BP6 and D9).

The main gate is test_browser_runs_leave_every_network_figure_byte_identical: one database of
Generic census, pulse and load runs for three products, calculated before and after browser
runs of all nine run targets (every outcome, two hours) are added, gives the same snapshot
byte for byte once the browser-only entries are removed.
"""

import hashlib
import json
from datetime import timedelta

import pytest
from calculated_browser_cases import (
    PROVIDERS,
    add_browser_runs,
    browser_transport,
    check_browser_target,
    check_group,
    first_difference,
    generic,
    network_only,
    tries,
    union_rate,
)
from calculated_cases import lenient_policy
from census_pulse_cases import batch, batch_run, product
from psycopg.rows import dict_row
from test_storage import measurement
from test_storage_v6 import load, save

from atlas.browser import contracts
from atlas.calculated.calculator import calculate
from atlas.calculated.inputs import cutoff
from atlas.storage import connect, ingest

pytest_plugins = ["test_storage", "calculated_browser_cases"]


# --- PostgreSQL: the equality gate ------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_runs_leave_every_network_figure_byte_identical(
    database, tmp_path, ai_sources
):
    """Fixture: 3 products x (census + 2 pulses + load), then 3 x 9 targets x 2 hours = 54
    browser runs (4 to 11 tries each, every K-BP2 outcome) with batch runs and geo rows."""
    as_of = await generic(database, tmp_path)
    policy = lenient_policy(tmp_path)
    before = calculate(database, policy, as_of=as_of)
    summaries = await add_browser_runs(database, tmp_path)
    after = calculate(database, policy, as_of=as_of)
    assert after["snapshot_id"] != before["snapshot_id"]
    a, b = network_only(before), network_only(after)
    # Byte-identical serializations (hashes keep a failure's report small: the first difference).
    digest = [hashlib.sha256(json.dumps(x).encode()).hexdigest() for x in (a, b)]
    assert digest[0] == digest[1], first_difference(a, b)
    # The comparison is not empty: three products with scores, uptime and breakdowns.
    assert [p["provider"] for p in a["products"]] == list(PROVIDERS)
    day = a["products"][0]["windows"]["24h"]
    assert day["metrics"]["completed"] and day["breakdowns"]["country"]
    assert day["scores"]["balanced"]["state"] and day["uptime"]["value"] is not None
    assert "browser" not in before["traffic"]["by_test"]
    assert after["traffic"]["by_test"]["browser"]["bytes_exact"] is False
    assert after["traffic"]["by_test"]["browser"]["runs"] == 54
    assert [n["provider"] for n in a["networks"]] == list(PROVIDERS)
    for p in [*after["products"], *after["networks"]]:  # networks: per target too (NET-SUM-1)
        found = p["windows"]["24h"]["targets"]
        listed = [t for t in found if t in contracts.BROWSER_TARGETS]
        assert listed == list(contracts.BROWSER_TARGETS)  # K-BP1 order, after the network ones
        assert list(found)[: -len(listed)] == ["generic_http", "generic_https"]
        for target in contracts.RUN_TARGETS:
            check_browser_target(found[target], target)
        minutes = p["windows"]["24h"]["metrics"]["time_slots"]["minutes"]
        check_group(found, union_rate(summaries, p["provider"], minutes))


@pytest.mark.asyncio
async def test_a_window_without_browser_runs_has_no_browser_entries(database, tmp_path, ai_sources):
    as_of = await generic(database, tmp_path)
    payload = calculate(database, lenient_policy(tmp_path), as_of=as_of)
    for p in payload["products"]:
        for w in p["windows"].values():
            assert list(w["targets"]) == ["generic_http", "generic_https"]
            assert all("browser" not in t for t in w["targets"].values())


@pytest.mark.asyncio
async def test_a_browser_run_in_the_cutoff_bucket_never_moves_the_cutoff(
    database, tmp_path, ai_sources
):
    """An off-grid cutoff whose bucket holds only a finished browser run stays at the bucket's
    start, as without it (else every network window would shift by 5 minutes), for an explicit
    cutoff and for a calculation of everything stored (latest)."""
    grid = await generic(database, tmp_path)  # after every network run's buckets
    requested = grid + timedelta(minutes=2, seconds=30)
    policy = lenient_policy(tmp_path)
    before = calculate(database, policy, as_of=requested)
    selection = tries("amazon_pdp", 1)
    directory = await measurement(
        tmp_path,
        requests=len(selection),
        target="amazon_pdp",
        provider=PROVIDERS[0],
        concurrency=1,
        requested_country="us",
        transport_factory=browser_transport(selection),
    )
    summary = load(directory)
    summary["started_at"] = (grid + timedelta(seconds=10)).isoformat(timespec="microseconds")
    summary["finished_at"] = (grid + timedelta(seconds=60)).isoformat(timespec="microseconds")
    assert len(summary["buckets"]) == 1
    summary["buckets"][0]["bucket_start"] = grid.isoformat(timespec="microseconds")
    save(directory, summary)
    with connect(database) as conn:
        batch_id = batch(conn, "browser", grid, country="US", target="amazon_pdp")
    key = ingest(directory, database, batch_id=batch_id)["run_key"]
    with connect(database) as conn:
        batch_run(
            conn,
            batch_id,
            product(conn, PROVIDERS[0]),
            grid + timedelta(seconds=60),
            run_key=key,
            requests=summary["completed"],
            successes=summary["successes"],
            bytes_down=summary["bytes_down"],
        )
        conn.row_factory = dict_row
        assert cutoff(conn, requested, True) == cutoff(conn, requested, False) == grid
    after = calculate(database, policy, as_of=requested)
    assert before["as_of"] == after["as_of"] == grid.isoformat()
    assert first_difference(network_only(before), network_only(after)) is None
