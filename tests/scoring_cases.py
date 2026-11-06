"""Shared helpers of the scoring tests (test_scoring.py): bucket rows, IP observations and
census runs around one instant."""

from datetime import UTC, datetime, timedelta

T = datetime(2026, 10, 3, 18, tzinfo=UTC)
HALF_HOUR = timedelta(minutes=30)


def row(
    slot,
    ms,
    *,
    n=100,
    successes=None,
    test="pulse",
    country="US",
    target="generic_http",
    census=(),
    run_key=None,
    region=None,
):
    """One bucket row: n requests in a time slot, every success at `ms` milliseconds; census
    names the census batches the run overlapped (a pulse or load wave during a census)."""
    successes = n if successes is None else successes
    return {
        "bucket_start": slot,
        "last_observed": slot,
        "slot": slot,
        "run_key": run_key,
        "test": test,
        "during_census": bool(census),
        "census_batches": list(census),
        "light": test in (None, "pulse") and not census,
        "country": country,
        "region": region,
        "target": target,
        "concurrency": 1000 if test == "load" else 25,
        "counters": {
            "completed": n,
            "successes": successes,
            "errors": {"proxy_connect": n - successes} if n > successes else {},
            "response_body_bytes": 0,
            "latency_histogram": {"bins": [[ms, successes]] if successes else []},
        },
    }


def observation(ip_id, country, asked, usage="residential", **fields):
    return {
        "ip_id": ip_id,
        "bucket_start": T,
        "first_seen": T,
        "last_seen": T,
        "count": 1,
        "product_first_seen": T,
        "requested_country": asked,
        "country_code": country,
        "usage_type": usage,
        "asn": ip_id,
        "matched": country is not None,
        **fields,
    }


def census_run(batch_id, start, minutes, product=1):
    return {
        "product_id": product,
        "batch_id": batch_id,
        "started_at": start,
        "finished_at": start + timedelta(minutes=minutes),
    }
