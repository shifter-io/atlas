"""Whether a country counts for a network (COUNTRY-MIN-IPS-1, the owner's decision of 2026-11-15).

A network's country counts only while its latest daily test there found at least MIN_IPS unique
IPs. Each daily test (a test_runs row whose summary has daily.MARKER = daily.DAILY) is one
verdict in country_verdicts (schema v16): its network (scope_stats.product_id), its country
(upper(pool_stats.requested_country)), its start (decided_at), its unique IPs and whether they
reach MIN_IPS (the generated column counts). The count is the test's own summary.unique_ips: the
distinct exit IPs of its successful answers whose exit DB-IP places in the requested country
(an address DB-IP does not match counts as in it; other-country exits, rate-limited and
no-inventory answers never count: answers.py, runner._record).

A verdict holds from its test's start until the next daily test of the same network and country
starts. Before a country's first daily test there is no verdict and the country does not count.
Everything measured for the network in the country while it does not count (its daily tests,
pulses, browser runs, outages and events) is left out of every published figure; nothing is
deleted, and the countries keep being tested so they can count again.
"""

from __future__ import annotations

from .daily import DAILY, MARKER

MIN_IPS = 10  # schema.sql's country_verdicts.counts repeats it
# outages.end_reason of an outage closed because its country stopped counting
END_REASON = "too_few_ips"

# The verdicts of the daily tests among the batch's new runs (incoming_runs, storage.ingest's
# temp table), in run order; a replay adds none. migrate_v15_v16.sql repeats it for every stored
# daily test.
RECORD = (
    "INSERT INTO country_verdicts(product_id,country_code,decided_at,run_key,unique_ips) "
    "SELECT s.product_id,upper(p.requested_country),t.started_at,t.run_key,"
    "coalesce((t.summary->>'unique_ips')::bigint,0) "
    "FROM test_runs t JOIN scope_stats s USING(scope_id) JOIN pool_stats p USING(pool_id) "
    f"WHERE t.summary->>'{MARKER}'='{DAILY}' AND upper(p.requested_country) ~ '^[A-Z]{{2}}$' "
    "AND t.run_key IN (SELECT run_key FROM incoming_runs) "
    "ORDER BY t.run_key ON CONFLICT DO NOTHING"
)


def daily(summary: dict) -> bool:
    """Whether a run's summary is a daily test's (it gets a verdict)."""
    return summary.get(MARKER) == DAILY


def record(conn, runs) -> None:
    """Queue the verdicts of the daily tests among runs (the batch's new runs, already in
    incoming_runs with their run keys); nothing when none is a daily test."""
    if any(daily(run.summary) for run in runs):
        conn.execute(RECORD)
