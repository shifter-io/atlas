"""CALC-PERF-3: the read step reads each run's scope once per scope (incremental.SCOPES), not with
every run (the view made a scope's JSON for each of its runs, every hour). The runs it returns are
the runs it returned before, key for key and in the same key order, and a scope's runs share one
dict."""

from datetime import UTC, datetime, timedelta

import pytest
from calculated_cases import lenient_policy
from psycopg.rows import dict_row
from test_calc_rollups import fixture
from test_calculated_hourly_reads import HOUR, floor_hour

from atlas import verdicts
from atlas.calculated import calculator, incremental, policy
from atlas.calculated.policy import load
from atlas.storage import RAW_DAYS, connect

pytest_plugins = ["test_storage"]


def former(conn, config, as_of):
    """Every run as the read made it before: its scope from the view, with the run."""
    roles = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in calculator.BATCH_ROLES.items())
    query = incremental.RUNS.format(summary="r.summary", roles=roles, where="")
    assert "NULL::jsonb AS scope," in query and "s.scope," not in query
    query = query.replace("NULL::jsonb AS scope,", "s.scope,")
    margin = as_of - timedelta(days=RAW_DAYS + 1)
    counting = verdicts.bounds(margin, as_of, policy.country_verdicts(config))
    return {r["run_key"]: r for r in conn.execute(query, counting)}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incremental", "full"])
async def test_each_scope_is_read_once_and_the_runs_are_as_before(mode, database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await fixture(database, tmp_path, end)
    config, method_id = load(lenient_policy(tmp_path))
    as_of = end - 2 * HOUR
    with connect(database) as conn:
        conn.row_factory = dict_row
        runs = incremental.read(conn, config, method_id, as_of, mode).runs
        before = former(conn, config, as_of)
    assert len(runs) >= 9
    for r in runs:
        old = before[r["run_key"]]
        assert r["scope"] == old["scope"] and r["scope"] is not None
        assert list(r)[: len(old)] == list(old)  # the same keys, the scope where it was
    scopes = {}
    for r in runs:
        assert scopes.setdefault(r["scope_id"], r["scope"]) is r["scope"]  # one dict a scope
    assert len(scopes) < len(runs)
