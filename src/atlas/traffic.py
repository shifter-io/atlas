"""Wire traffic of the census, pulse and load tests, and of browser runs (atlas report
traffic, GET /v1/traffic).

Each provider's run in a batch counts once. A run whose artifact was ingested uses its stored
run_buckets (requests = completed, exact wire bytes) when every bucket carries the v6 outcome
counters; any other run uses the batch_runs counters (final, or the latest progress of a run
that is still going or never uploaded). Time filters select batches by their scheduled slot.
GB are decimal: 10^9 bytes. A browser run's bytes are the browser's estimates (K-BP4
bytes_down_est), so by_test has a browser entry, with bytes_exact false, only when there are
browser runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from psycopg.rows import dict_row

GB = 10**9
TESTS = ("census", "pulse", "load")
BROWSER = "browser"
# The by_test keys the calculated snapshot keeps (traffic.by_test).
SNAPSHOT = ("runs", "requests", "bytes_up", "bytes_down", "bytes_exact")
TERMINAL = ("done", "failed", "skipped", "killed")


def parse_time(value):
    """ISO date or timestamp; a value without a timezone is UTC."""
    if value is None or value == "":
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def totals(rows):
    runs = sum(r["runs"] for r in rows)
    requests = sum(r["requests"] for r in rows)
    up = sum(r["bytes_up"] for r in rows)
    down = sum(r["bytes_down"] for r in rows)
    return {
        "runs": runs,
        "requests": requests,
        "bytes_up": up,
        "bytes_down": down,
        "bytes_total": up + down,
        "gb_up": up / GB,
        "gb_down": down / GB,
        "gb_total": (up + down) / GB,
        "avg_bytes_per_request": (up + down) / requests if requests else None,
    }


@dataclass(frozen=True)
class TrafficInputs:
    """summary()'s filters, each None for all: test kind, provider ("provider" or
    "provider/product"), country, region slug, the scheduled-slot window [start, end), and
    finished_before, which keeps only runs that ended before that moment (the snapshot uses it,
    so a calculation does not see live counters)."""

    test: str | None = None
    provider: str | None = None
    country: str | None = None
    region: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    finished_before: datetime | None = None


def where(inputs):
    """The WHERE clauses and their parameters for summary()'s filters."""
    clauses, params = ["br.status<>'queued'"], []
    if inputs.test:
        if inputs.test not in (*TESTS, BROWSER):
            raise ValueError("test must be census, pulse, load or browser")
        clauses.append("b.kind=%s")
        params.append(inputs.test)
    if inputs.provider:
        name, _, product = inputs.provider.partition("/")
        clauses.append("p.provider=%s")
        params.append(name)
        if product:
            clauses.append("p.name=%s")
            params.append(product)
    if inputs.country:
        clauses.append("b.country_code=%s")
        params.append(inputs.country.upper())
    if inputs.region:
        clauses.append("r.slug=%s")
        params.append(inputs.region)
    if inputs.start is not None:
        clauses.append("b.scheduled_for>=%s")
        params.append(inputs.start)
    if inputs.end is not None:
        clauses.append("b.scheduled_for<%s")
        params.append(inputs.end)
    if inputs.finished_before is not None:
        clauses.append("br.status=ANY(%s) AND br.finished_at<%s")
        params += [list(TERMINAL), inputs.finished_before]
    return clauses, params


def summary(conn, inputs):
    """Traffic per test and provider product, per provider, per test and in total, for the
    runs `inputs` (a TrafficInputs) selects."""
    clauses, params = where(inputs)
    rows = (
        conn.cursor(row_factory=dict_row)
        .execute(
            "SELECT b.kind AS test,p.provider,p.name AS product,count(*)::bigint AS runs,"
            "count(x.requests)::bigint AS artifact_runs,"
            "coalesce(sum(coalesce(x.requests,br.requests)),0)::bigint AS requests,"
            "coalesce(sum(coalesce(x.bytes_up,br.bytes_up)),0)::bigint AS bytes_up,"
            "coalesce(sum(coalesce(x.bytes_down,br.bytes_down)),0)::bigint AS bytes_down "
            "FROM batch_runs br JOIN test_batches b USING(batch_id) "
            "JOIN products p USING(product_id) JOIN runner_regions r ON r.region_id=b.region_id "
            "LEFT JOIN test_runs t ON t.run_key=br.run_key "
            # A run's buckets lie in its own span: each run_buckets partition is read only for
            # the runs of its day, not probed for every run (CALC-PERF-1).
            "LEFT JOIN LATERAL (SELECT sum(rb.completed) AS requests,sum(rb.bytes_up) AS bytes_up,"
            "sum(rb.bytes_down) AS bytes_down FROM run_buckets rb WHERE rb.run_key=br.run_key "
            "AND rb.bucket_start>=date_bin('5 minutes',t.started_at,"
            "'1970-01-01 00:00:00+00'::timestamptz) "
            "AND rb.bucket_start<=t.finished_at "
            "HAVING bool_and(rb.outcome_counters)) x ON true WHERE "
            + " AND ".join(clauses)
            + " GROUP BY 1,2,3 ORDER BY 1,2,3",
            params,
        )
        .fetchall()
    )
    detail = [r | totals([r]) for r in rows]
    providers = sorted({r["provider"] for r in rows})
    by_test = {kind: totals([r for r in rows if r["test"] == kind]) for kind in TESTS}
    if browser := [r for r in rows if r["test"] == BROWSER]:
        by_test[BROWSER] = totals(browser) | {"bytes_exact": False}
    return {
        "unit": "GB = 10^9 bytes",
        "period": {"from": inputs.start, "to": inputs.end},
        "filters": {
            "test": inputs.test,
            "provider": inputs.provider,
            "country": inputs.country,
            "region": inputs.region,
        },
        "rows": detail,
        "by_provider": [
            {"provider": name} | totals([r for r in rows if r["provider"] == name])
            for name in providers
        ],
        "by_test": by_test,
        "total": totals(rows),
    }


def snapshot(by_test):
    """by_test as the calculated snapshot keeps it: per test its runs, requests and bytes (and
    the browser entry's bytes_exact)."""
    return {test: {k: t[k] for k in SNAPSHOT if k in t} for test, t in by_test.items()}


def table(report):
    """Plain-text rendering for the CLI: one line per test and product, then totals."""
    header = (
        "TEST",
        "PROVIDER",
        "PRODUCT",
        "RUNS",
        "REQUESTS",
        "GB UP",
        "GB DOWN",
        "GB TOTAL",
        "AVG B/REQ",
    )

    def line(test, provider, product, t):
        average = t["avg_bytes_per_request"]
        return (
            test,
            provider,
            product,
            f"{t['runs']:,}",
            f"{t['requests']:,}",
            f"{t['gb_up']:.3f}",
            f"{t['gb_down']:.3f}",
            f"{t['gb_total']:.3f}",
            "-" if average is None else f"{average:,.0f}",
        )

    lines = [line(r["test"], r["provider"], r["product"], r) for r in report["rows"]]
    lines += [line("all", p["provider"], "", p) for p in report["by_provider"]]
    lines += [line(k, "all", "", t) for k, t in report["by_test"].items()]
    lines.append(line("all", "all", "", report["total"]))
    widths = [max(len(row[i]) for row in [header, *lines]) for i in range(len(header))]
    numeric = range(3, len(header))
    return "\n".join(
        "  ".join(
            cell.rjust(widths[i]) if i in numeric else cell.ljust(widths[i])
            for i, cell in enumerate(row)
        ).rstrip()
        for row in [header, *lines]
    )
