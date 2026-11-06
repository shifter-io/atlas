"""The daily latency-under-load reference country of every runner region (`load_references`),
chosen from the trailing light-load p50 of the census target and stored in load_references.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from ..calculated.aggregate import blend, census_overlaps
from .db import setting
from .sqlrows import _rows


def _countries(value: Any, name: str) -> dict[str, str]:
    """A {region slug: country code} setting, upper-cased."""
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and len(v) == 2 and v.isalpha()
        for k, v in value.items()
    ):
        raise ValueError(f"{name} must map region slugs to two-letter country codes")
    return {k: v.upper() for k, v in value.items()}


async def _reference_latency(
    conn: psycopg.AsyncConnection, day_start: datetime, days: int
) -> dict[str, dict[str, Any]]:
    """Per country, the light-load p50 of the census target over [day_start - days, day_start).

    From every product's done pulses of a census target whose run was ingested, except those
    that ran during a census of the same product (census_overlaps(): from stored run times). Each
    product's merged histogram weighs the same (blend()). Returns {country: {"p50", "successes"}}.
    """
    lo = day_start - timedelta(days=days)
    times = (
        "coalesce(r.started_at,br.started_at) AS started_at,"
        "least(coalesce(r.finished_at,br.finished_at,%(hi)s),%(hi)s) AS finished_at "
        "FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "LEFT JOIN test_runs r ON r.run_key=br.run_key "
    )
    bounds = {"lo": lo, "hi": day_start, "margin": lo - timedelta(days=1)}
    pulses = await _rows(
        conn,
        f"SELECT br.product_id,br.batch_id,br.run_key,trim(b.country_code) AS country,{times}"
        "JOIN targets t ON t.target_id=b.target_id AND t.census WHERE b.kind='pulse' "
        "AND br.status='done' AND br.run_key IS NOT NULL AND b.scheduled_for>=%(lo)s "
        "AND b.scheduled_for<%(hi)s",
        bounds,
    )
    if not pulses:
        return {}
    census = await _rows(
        conn,
        f"SELECT br.product_id,br.batch_id,{times}WHERE b.kind='census' "
        "AND br.started_at IS NOT NULL AND br.started_at<%(hi)s "
        "AND coalesce(br.finished_at,%(hi)s)>=%(margin)s",
        bounds,
    )
    flagged = {o["run_key"] for o in census_overlaps(census, pulses)}
    clean = {p["run_key"]: p for p in pulses if p["run_key"] not in flagged}
    histograms: dict[str, dict[int, Counter]] = {}
    for row in await _rows(
        conn,
        "SELECT b.run_key,h.ms,sum(h.n)::bigint AS n FROM run_buckets b CROSS JOIN LATERAL "
        "histogram(b.histogram_steps,b.histogram_counts::bigint[]) h WHERE b.run_key=ANY(%s) "
        "AND b.bucket_start>=%s GROUP BY 1,2",
        (sorted(clean), lo - timedelta(hours=1)),
    ):
        run = clean[row["run_key"]]
        products = histograms.setdefault(run["country"], {})
        products.setdefault(run["product_id"], Counter())[row["ms"]] += row["n"]
    return {
        country: {
            "p50": blend(products.values())["p50"],
            "successes": sum(sum(h.values()) for h in products.values()),
        }
        for country, products in histograms.items()
    }


async def load_references(conn: psycopg.AsyncConnection, now: datetime) -> list[dict]:
    """Today's (UTC) latency-under-load reference country of every runner region with enabled
    countries, chosen on first use and stored in load_references (so every load batch of the day
    and every later calculation sees the same choice).

    Per region: an override (load.reference_overrides) that is one of its enabled countries
    ('override'); else the enabled country with the lowest light-load p50 over the trailing
    load.reference_days among those with at least load.reference_min_successes successes, ties
    broken by country code ('fastest'); else its default (load.reference_defaults, 'default');
    else its first enabled country by code ('first_enabled'). A stored choice whose country was
    disabled since is skipped (no load wave there today).
    """
    day = now.astimezone(UTC).date()
    regions = await _rows(
        conn,
        "SELECT r.region_id,r.slug,array_agg(trim(c.country_code) ORDER BY c.country_code) "
        "AS countries FROM runner_regions r JOIN countries c ON c.region_id=r.region_id "
        "AND c.enabled WHERE r.enabled GROUP BY 1,2 ORDER BY 1",
    )
    chosen = {
        row["region_id"]: row
        for row in await _rows(conn, "SELECT * FROM load_references WHERE day=%s", (day,))
    }
    missing = [r for r in regions if r["region_id"] not in chosen]
    if missing:
        overrides = _countries(
            await setting(conn, "load.reference_overrides"), "load.reference_overrides"
        )
        defaults = _countries(
            await setting(conn, "load.reference_defaults"), "load.reference_defaults"
        )
        minimum = int(await setting(conn, "load.reference_min_successes"))
        day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        stats = await _reference_latency(
            conn, day_start, int(await setting(conn, "load.reference_days"))
        )
        for region in missing:
            countries = region["countries"]
            candidates = sorted(
                (stats[c]["p50"], c)
                for c in countries
                if c in stats and stats[c]["p50"] is not None and stats[c]["successes"] >= minimum
            )
            if overrides.get(region["slug"]) in countries:
                country, basis = overrides[region["slug"]], "override"
            elif candidates:
                country, basis = candidates[0][1], "fastest"
            elif defaults.get(region["slug"]) in countries:
                country, basis = defaults[region["slug"]], "default"
            else:
                country, basis = countries[0], "first_enabled"
            await conn.execute(
                "INSERT INTO load_references(day,region_id,country_code,basis,p50_ms,successes,"
                "candidates) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    day,
                    region["region_id"],
                    country,
                    basis,
                    stats.get(country, {}).get("p50"),
                    stats.get(country, {}).get("successes"),
                    len(candidates),
                ),
            )
        chosen = {
            row["region_id"]: row
            for row in await _rows(conn, "SELECT * FROM load_references WHERE day=%s", (day,))
        }
    return [
        chosen[r["region_id"]]
        for r in regions
        if r["region_id"] in chosen
        and chosen[r["region_id"]]["country_code"].strip() in r["countries"]
    ]
