"""The hourly calculation's observation reads (CALC-INC-1a regression), and the seeded history the
incremental calculation's tests use (test_calculated_incremental.py).

History places measured runs back in time in the database, each with seeded random observations
of documentation IPs labelled in two DB-IP releases (test_sqlwindow's labels: unenriched IPs, IPs
in two countries of one network, empty and NULL labels) and released estimated hours. The
regression: the `calculate` command (the hourly job) read every observation row of the 30-day
window every hour, as `calculate --full` (the oracle) still does; `calculate`, incremental by
default, reads none when no test changed.
"""

import random
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from calculated_browser_cases import browser_run
from calculated_cases import lenient_policy
from census_pulse_cases import batch, batch_run, product
from test_sqlwindow import _labels
from test_storage import measurement
from test_storage_v6 import census

from atlas.browser import contracts
from atlas.calculated import store
from atlas.cli import main
from atlas.partitions import partition
from atlas.storage import connect, ingest, seed

pytest_plugins = ["test_storage"]

HOUR, DAY, BUCKET = timedelta(hours=1), timedelta(days=1), timedelta(minutes=5)
# Statements that return observation rows to Python (releases.OBSERVATIONS and its run filter).
RAW = "host(a.ip) AS ip"
PRODUCT_IPS = (
    "DELETE FROM product_ips;INSERT INTO product_ips SELECT s.product_id,o.ip_id,"
    "min(o.first_seen),max(o.last_seen),sum(o.count),count(DISTINCT o.run_key) "
    "FROM ip_observations o JOIN test_runs r USING(run_key) JOIN scope_stats s USING(scope_id) "
    "GROUP BY 1,2"
)
ESTIMATED = (
    "INSERT INTO estimated_hours(product_id,country_code,day,hour,run_key,measured_at,"
    "measured_ips,measured_requests,hour_factor,unique_ips,requests,day_unique_ips,curve,"
    "formula_version,released_at) SELECT s.product_id,"
    "upper(s.scope->'config'->>'requested_country'),"
    "(r.started_at AT TIME ZONE 'UTC')::date,h,r.run_key,r.started_at,%s,40,1,%s,%s,%s,"
    "'country',1,((r.started_at AT TIME ZONE 'UTC')::date+make_interval(hours=>h+1)) "
    "AT TIME ZONE 'UTC' FROM test_runs r JOIN scope_details s "
    "USING(scope_id) CROSS JOIN generate_series(0,23) h WHERE r.run_key=%s "
    "AND s.scope->'config'->>'requested_country' IS NOT NULL ON CONFLICT DO NOTHING"
)


def floor_hour(moment):
    return moment.replace(minute=0, second=0, microsecond=0)


class History:
    """Runs placed `back` before `end` (a whole hour), with synthetic observations."""

    def __init__(self, database, tmp_path, end, seed_value=20261105):
        self.database, self.tmp_path, self.end = database, tmp_path, end
        self.rng = random.Random(seed_value)
        store.initialize(database)
        seed(database)
        with connect(database) as conn:
            self.releases = [
                conn.execute(
                    "INSERT INTO dbip_releases(sha256,metadata) VALUES (%s,'{}') "
                    "RETURNING release_id",
                    (f"{n:064x}",),
                ).fetchone()[0]
                for n in (21, 22)
            ]
            addresses = [f"198.51.100.{n}" for n in range(1, 61)]
            addresses += [f"2001:db8::{n:x}" for n in range(1, 11)]
            self.ips = [
                conn.execute(
                    "INSERT INTO ip_addresses(ip) VALUES (%s) RETURNING ip_id", (address,)
                ).fetchone()[0]
                for address in addresses
            ]
            _labels(conn, self.rng, self.releases, self.ips)

    async def daily(self, provider, country, back, *, split=False):
        directory = await measurement(
            self.tmp_path, requests=40, provider=provider, requested_country=country
        )
        return self.settle(ingest(directory, self.database)["run_key"], back, split=split)

    async def scheduled(self, kind, provider, back):
        """A census (with its geo artifact) or pulse run in its batch, with its batch run."""
        with connect(self.database) as conn:
            batch_id = batch(conn, kind, self.end - back - timedelta(minutes=1))
        if kind == "census":
            directory, _ = await census(self.tmp_path, provider=provider, requests=40)
        else:
            directory = await measurement(
                self.tmp_path, requests=40, provider=provider, requested_country="de"
            )
        key = self.settle(ingest(directory, self.database, batch_id=batch_id)["run_key"], back)
        with connect(self.database) as conn:
            finished = conn.execute(
                "SELECT finished_at FROM test_runs WHERE run_key=%s", (key,)
            ).fetchone()[0]
            batch_run(conn, batch_id, product(conn, provider), finished, run_key=key, requests=40)
        return key

    async def browser(self, provider, hours):
        """A browser run of a page target in its batch (no exit IPs: no observation)."""
        target = contracts.PAGE_TARGETS[0]
        directory, summary = await browser_run(self.tmp_path, provider, target, hours)
        started = datetime.fromisoformat(summary["started_at"])
        with connect(self.database) as conn:
            batch_id = batch(conn, "browser", started, country="US", target=target)
        key = ingest(directory, self.database, batch_id=batch_id)["run_key"]
        with connect(self.database) as conn:
            finished = datetime.fromisoformat(summary["finished_at"])
            batch_run(conn, batch_id, product(conn, provider), finished, run_key=key)
        return key

    def settle(self, run_key, back, *, split=False):
        """Move the run so its first bucket starts `back` before end (split: and add a second
        bucket 5 minutes later), and give it seeded observations, releases and estimates."""
        with connect(self.database) as conn:
            first = conn.execute(
                "SELECT min(bucket_start) FROM run_buckets WHERE run_key=%s", (run_key,)
            ).fetchone()[0]
            delta = first - (self.end - back)
            for table in ("run_buckets", "run_geo_buckets", "ip_observations"):
                partition(conn, table, self.end - back)
                conn.execute(
                    f"UPDATE {table} SET bucket_start=bucket_start-%s WHERE run_key=%s",
                    (delta, run_key),
                )
            conn.execute(
                "UPDATE test_runs SET started_at=started_at-%s,finished_at=finished_at-%s+%s "
                "WHERE run_key=%s",
                (delta, delta, BUCKET if split else timedelta(0), run_key),
            )
            if split:
                partition(conn, "run_buckets", self.end - back + BUCKET)
                conn.execute(
                    "CREATE TEMP TABLE split ON COMMIT DROP AS "
                    "SELECT * FROM run_buckets WHERE run_key=%s",
                    (run_key,),
                )
                conn.execute("UPDATE split SET bucket_start=bucket_start+interval '5 minutes'")
                conn.execute("INSERT INTO run_buckets SELECT * FROM split")
            self.observe(conn, run_key)
            conn.execute(PRODUCT_IPS)
            figures = [self.rng.randint(5, 30) for _ in range(4)]
            conn.execute(ESTIMATED, (*figures, run_key))
        return run_key

    def observe(self, conn, run_key):
        """Replace the run's observations with seeded random ones in each of its buckets (fewer
        than its successes), and give it zero, one or both releases."""
        conn.execute("DELETE FROM ip_observations WHERE run_key=%s", (run_key,))
        buckets = conn.execute(
            "SELECT bucket_start FROM run_buckets WHERE run_key=%s ORDER BY 1", (run_key,)
        ).fetchall()
        for (bucket,) in buckets:
            partition(conn, "ip_observations", bucket)
            for ip_id in self.rng.sample(self.ips, self.rng.randint(4, 12)):
                conn.execute(
                    "INSERT INTO ip_observations VALUES (%s,%s,%s,%s,%s,%s)",
                    (bucket, run_key, ip_id, self.rng.randint(1, 3), bucket, bucket + BUCKET / 5),
                )
        for release in self.rng.choice([[], [0], [1], [0, 1]]):
            conn.execute(
                "INSERT INTO run_enrichments VALUES (%s,%s,'fixture','{}')",
                (run_key, self.releases[release]),
            )


def statements(monkeypatch):
    """Record (statement, rows) of every statement a cursor runs."""
    log = []
    original = psycopg.Cursor.execute

    def execute(self, query, params=None, **kwargs):
        result = original(self, query, params, **kwargs)
        log.append((query if isinstance(query, str) else repr(query), self.rowcount))
        return result

    monkeypatch.setattr(psycopg.Cursor, "execute", execute)
    return log


def observation_rows(log):
    return sum(rows for query, rows in log if RAW in query)


async def daily_history(database, tmp_path, end):
    """Daily tests only (every window has a list signature)."""
    h = History(database, tmp_path, end, seed_value=7)
    for provider, country, back in (("alpha", "us", 20), ("beta", "us", 30), ("alpha", "de", 50)):
        await h.daily(provider, country, back * HOUR)
    return h


@pytest.mark.asyncio
async def test_an_hour_without_new_tests_reads_no_observation_row(
    database, tmp_path, monkeypatch, capsys
):
    """The regression: `calculate --full` reads every observation row of the window each hour;
    `calculate --incremental`, the default, reads none when no test changed."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    await daily_history(database, tmp_path, end)
    policy = str(lenient_policy(tmp_path))
    with connect(database) as conn:
        stored = conn.execute(
            "SELECT count(*) FROM ip_observations WHERE bucket_start>=%s AND bucket_start<%s",
            (end - 30 * DAY, end),
        ).fetchone()[0]
    args = ["calculate", "--database-file", str(database), "--methodology", policy, "--as-of"]
    assert main([*args, (end - HOUR).isoformat(), "--full"]) == 0  # its lists, with signatures
    log = statements(monkeypatch)
    assert main([*args, end.isoformat(), "--full"]) == 0
    assert stored > 0 and observation_rows(log) == stored  # the full calculation: every row
    log.clear()
    assert main([*args, end.isoformat(), "--incremental"]) == 0
    assert observation_rows(log) == 0, f"read {observation_rows(log)} of {stored}"
    capsys.readouterr()
