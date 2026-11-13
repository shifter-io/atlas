"""TAPE-FAST-1's measurement: the tape's fast replay over two days of pulses at the real shape, on a
throwaway database (an empty one; never a shared or production one).

It writes 18 networks' half-hourly pulses of generic_http and generic_https in --countries
countries over two days (200 countries: 691,200 pulse runs, #837's count of about 690,000), each
pulse a run with one latency bucket and a success rate within +-3 points of its network's; one
2-hour outage per network and day in 5 countries (both targets), one latency spike per network
and day, and a load batch per runner region and slot whose runs' exits are geolocated (three
networks through a hosting ASN). Then it runs the fast `daily tape` over the two days and prints
one JSON line: the runs, the events, the minutes and statements per hour of pulses, and the
projected minutes for the two days at --round-trip-ms per statement (the tor1 manager's link to
proxy_reviews is about 9 ms).

    uv run python scripts/scale/tape_measure.py --database-file <throwaway.toml> [--countries 200]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

from atlas import detect_replay
from atlas.daily import tape
from atlas.storage import connect, initialize, partition, seed

START = datetime(2026, 11, 1, tzinfo=UTC)
DAYS, NETWORKS = 2, 18
SETUP = """
INSERT INTO providers SELECT format('net%%s',n) FROM generate_series(1,18) n;
INSERT INTO products(provider,name) SELECT id,'residential' FROM providers;
INSERT INTO pool_stats(product_id,pool_hash,target_id)
 SELECT product_id,t,t FROM products,unnest(ARRAY['generic_http','generic_https']) t;
INSERT INTO scope_stats(pool_id,product_id,target,scope_hash,concurrency,timeout_seconds)
 SELECT pool_id,product_id,target_id,target_id,25,15 FROM pool_stats;
INSERT INTO asn_names(asn,name) VALUES (24940,'hosting'),(3320,'access');
INSERT INTO enrichment_networks(asn_name_id,usage_type)
 SELECT asn_name_id,CASE asn WHEN 24940 THEN 'hosting' ELSE 'isp' END FROM asn_names;
INSERT INTO place_names DEFAULT VALUES ON CONFLICT DO NOTHING;
INSERT INTO enrichment_locations(place_id) SELECT min(place_id) FROM place_names
 ON CONFLICT DO NOTHING;
CREATE TEMP TABLE picked AS SELECT country_code,region_id,
 row_number() OVER (ORDER BY country_code) - 1 AS i FROM countries
 ORDER BY country_code LIMIT %(countries)s;
INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,start_at,started_at,
 finished_at,status)
 SELECT 'pulse',c.country_code,c.region_id,t,s,s,s,
  s+interval '90 seconds'+((hashtext(c.country_code||t||s::text)::bigint%%60+60)%%60)
  *interval '1 second','done'
 FROM picked c,unnest(ARRAY['generic_http','generic_https']) t,
  generate_series(%(start)s::timestamptz,%(end)s::timestamptz-interval '30 minutes',
  interval '30 minutes') s;
INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,start_at,started_at,
 finished_at,status)
 SELECT DISTINCT ON (c.region_id,s) 'load',c.country_code,c.region_id,'generic_http',s,
  s+interval '75 seconds',s+interval '75 seconds',s+interval '200 seconds','done'
 FROM picked c,generate_series(%(start)s::timestamptz,%(end)s::timestamptz-interval '30 minutes',
  interval '30 minutes') s ORDER BY c.region_id,s,c.country_code;
INSERT INTO test_runs(run_id,scope_id,started_at,finished_at,status,summary_sha256,summary,
 batch_id)
 SELECT gen_random_uuid(),s.scope_id,b.started_at,b.finished_at,'completed','-','{}',b.batch_id
 FROM test_batches b JOIN scope_stats s ON s.target=b.target_id;
CREATE TEMP TABLE shaped AS
 SELECT r.run_key,r.batch_id,s.product_id,b.kind,b.started_at,b.finished_at,
  substr(p.provider,4)::integer AS n,c.i,
  floor(extract(epoch FROM b.scheduled_for-%(start)s::timestamptz)/1800)::integer AS slot,
  (hashtext(r.run_key::text)::bigint%%7+7)%%7-3 AS jitter
 FROM test_runs r JOIN test_batches b USING(batch_id) JOIN scope_stats s USING(scope_id)
 JOIN products p USING(product_id) JOIN picked c USING(country_code);
INSERT INTO batch_runs(batch_id,product_id,status,started_at,finished_at,requests,successes,
 run_key)
 SELECT batch_id,product_id,'done',started_at,finished_at,
  CASE kind WHEN 'pulse' THEN 100 ELSE 1000 END,
  CASE WHEN kind='pulse' AND (i-n*5+1000)%%%(countries)s<5 AND slot%%48 BETWEEN 10+n AND 13+n
   THEN 0 ELSE (CASE kind WHEN 'pulse' THEN 1 ELSE 10 END)*(80+n+jitter) END,run_key
 FROM shaped;
INSERT INTO run_buckets(bucket_start,run_key,completed,successes,response_body_bytes,errors,
 status_codes,histogram_steps,histogram_counts)
 SELECT date_bin('5 minutes',x.started_at,'2000-01-01'),x.run_key,br.requests,br.successes,0,
  '{}','{}',ARRAY[(200+20*x.n+x.jitter*5)*(CASE WHEN x.slot%%48=20+n THEN 3 ELSE 1 END)],
  ARRAY[greatest(br.successes,1)]
 FROM shaped x JOIN batch_runs br USING(run_key);
INSERT INTO run_geo_buckets
 SELECT date_bin('5 minutes',x.started_at,'2000-01-01'),x.run_key,
  (SELECT min(location_id) FROM enrichment_locations),
  (SELECT network_id FROM enrichment_networks e JOIN asn_names a USING(asn_name_id)
   WHERE a.asn=CASE WHEN x.n<=3 THEN 24940 ELSE 3320 END),
  br.requests,br.successes,0,0,0,0,0,ARRAY[200],ARRAY[br.successes]
 FROM shaped x JOIN batch_runs br USING(run_key) WHERE x.kind='load';
"""


def build(database: Path, countries: int) -> int:
    """The two days' batches and runs (module docstring); returns the pulse runs."""
    initialize(database)
    with connect(database) as conn:
        if conn.execute("SELECT count(*) FROM test_batches").fetchone()[0]:
            sys.exit("refused: the database has batches; give an empty, throwaway one")
    seed(database)
    end = START + timedelta(days=DAYS)
    with connect(database) as conn:
        for day in range(DAYS + 1):
            for table in ("run_buckets", "run_geo_buckets", "test_events"):
                partition(conn, table, START + timedelta(days=day))
        params = {"countries": countries, "start": START, "end": end}
        psycopg.ClientCursor(conn).execute(SETUP, params)  # one script, bound client-side
        conn.execute("ANALYZE")
        return conn.execute(
            "SELECT count(*) FROM batch_runs JOIN test_batches USING(batch_id) WHERE kind='pulse'"
        ).fetchone()[0]


def measured(database: Path, round_trip_ms: float) -> dict:
    """The fast replay over the two days, timed and with its statements counted per hour."""
    hours, statements, real_execute, real_hour = [], [0], psycopg.Cursor.execute, None

    def execute(self, *args, **options):
        statements[0] += 1
        return real_execute(self, *args, **options)

    def hour(self, upto, end, step):
        before, began = statements[0], time.monotonic()
        found = real_hour(self, upto, end, step)
        hours.append((time.monotonic() - began, statements[0] - before))
        return found

    real_hour = detect_replay.Replay.hour
    psycopg.Cursor.execute, detect_replay.Replay.hour = execute, hour
    try:
        with connect(database) as conn:
            report = tape.replay(conn, START, START + timedelta(days=DAYS))
    finally:
        psycopg.Cursor.execute, detect_replay.Replay.hour = real_execute, real_hour
    seconds = [s for s, _ in hours]
    counts = [n for _, n in hours]
    wire = round_trip_ms / 1000 * statements[0]
    return {
        "events": report["events"],
        "outages": len(report["outages"]),
        "hours": len(hours),
        "seconds": report["seconds"],
        "minutes_per_hour": {"mean": sum(seconds) / len(hours) / 60, "max": max(seconds) / 60},
        "statements_per_hour": {"mean": sum(counts) / len(hours), "max": max(counts)},
        "statements": statements[0],
        "projected_minutes": (report["seconds"] + wire) / 60,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-file", type=Path, required=True)
    parser.add_argument("--countries", type=int, default=200)
    parser.add_argument("--round-trip-ms", type=float, default=9.0)
    args = parser.parse_args()
    began = time.monotonic()
    runs = build(args.database_file, args.countries)
    built = time.monotonic() - began
    found = measured(args.database_file, args.round_trip_ms)
    print(json.dumps({"pulse_runs": runs, "build_seconds": round(built, 1)} | found))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
