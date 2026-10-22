"""Wire traffic of the census, pulse and load tests (proxy-index report traffic,
GET /v1/traffic).

Each provider's run in a batch counts once. A run whose artifact was ingested uses its stored
run_buckets (requests = completed, exact wire bytes) when every bucket carries the v6 outcome
counters; any other run uses the batch_runs counters (final, or the latest progress of a run
that is still going or never uploaded). Time filters select batches by their scheduled slot.
GB are decimal: 10^9 bytes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from psycopg.rows import dict_row

GB = 10**9
TESTS = ("census", "pulse", "load")
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


def summary(
    conn,
    *,
    test=None,
    provider=None,
    country=None,
    region=None,
    start=None,
    end=None,
    finished_before=None,
):
    """Traffic per test and provider product, per provider, per test and in total.

    provider is "provider" or "provider/product"; finished_before keeps only runs that ended
    before that moment (the snapshot uses it, so a calculation does not see live counters).
    """
    clauses, params = ["br.status<>'queued'"], []
    if test:
        if test not in TESTS:
            raise ValueError("test must be census, pulse or load")
        clauses.append("b.kind=%s")
        params.append(test)
    if provider:
        name, _, product = provider.partition("/")
        clauses.append("p.provider=%s")
        params.append(name)
        if product:
            clauses.append("p.name=%s")
            params.append(product)
    if country:
        clauses.append("b.country_code=%s")
        params.append(country.upper())
    if region:
        clauses.append("r.slug=%s")
        params.append(region)
    if start is not None:
        clauses.append("b.scheduled_for>=%s")
        params.append(start)
    if end is not None:
        clauses.append("b.scheduled_for<%s")
        params.append(end)
    if finished_before is not None:
        clauses.append("br.status=ANY(%s) AND br.finished_at<%s")
        params += [list(TERMINAL), finished_before]
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
            "LEFT JOIN LATERAL (SELECT sum(rb.completed) AS requests,sum(rb.bytes_up) AS bytes_up,"
            "sum(rb.bytes_down) AS bytes_down FROM run_buckets rb WHERE rb.run_key=br.run_key "
            "HAVING bool_and(rb.outcome_counters)) x ON true WHERE "
            + " AND ".join(clauses)
            + " GROUP BY 1,2,3 ORDER BY 1,2,3",
            params,
        )
        .fetchall()
    )
    detail = [r | totals([r]) for r in rows]
    providers = sorted({r["provider"] for r in rows})
    return {
        "unit": "GB = 10^9 bytes",
        "period": {"from": start, "to": end},
        "filters": {"test": test, "provider": provider, "country": country, "region": region},
        "rows": detail,
        "by_provider": [
            {"provider": name} | totals([r for r in rows if r["provider"] == name])
            for name in providers
        ],
        "by_test": {kind: totals([r for r in rows if r["test"] == kind]) for kind in TESTS},
        "total": totals(rows),
    }


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
