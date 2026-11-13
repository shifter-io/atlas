"""COVERAGE-IPS-RANK-1's measurement: the time netlists.rank() adds to a build, on synthetic
network lists of a real shape: --networks networks of one kind, each with a list per window
(24h, 7d, 30d) of --rows City x ASN rows, at places that other networks share (about two thirds
of a list's places are in every other list): 1.5 x --rows places in 50 countries and 977 ASNs,
real label rows that rank() keys its places by. Each round ranks every list, timed, then clears
the ranks and vacuums.
On a disposable server with PG_TEST_ADMIN_FILE set:
    python tests/ips_rank_measure.py [--networks 18] [--rows 25000 100000] [--rounds 3]
prints one JSON line per size: rows ranked and the median seconds.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).parent))

from ingest_cases import credentials_file  # noqa: E402

from atlas import storage  # noqa: E402
from atlas.calculated import netlists, store  # noqa: E402
from atlas.providers.credentials import read_credentials  # noqa: E402

WINDOWS = {"24h": 24, "7d": 168, "30d": 720}
FILL = (
    "INSERT INTO calculated_list_rows(list_id,dimension,position,place_id,asn_label_id,ips,"
    "observations) SELECT %(list)s,'y',g-1,1+(g+%(shift)s)%%(%(rows)s*3/2),1+g%%977,"
    "1+(hashint4(g+%(shift)s)&1023),1 FROM generate_series(1,%(rows)s) g"
)

# Fresh label tables, so their ids run from 1: places, then ASN names and their labels.
LABELS = (
    "INSERT INTO place_names(country_code,country,region_code,region,city) SELECT "
    "'C'||(i%%50),'Country','R'||(i%%400),'Region','City '||i "
    "FROM generate_series(1,%(places)s) i ORDER BY i",
    "INSERT INTO asn_names(asn,name) SELECT i,'Net '||i FROM generate_series(1,977) i ORDER BY i",
    "INSERT INTO asn_labels(asn_name_id,usage_type) SELECT asn_name_id,'isp' FROM asn_names "
    "ORDER BY asn_name_id",
)


def lists(conn, networks, rows):
    """Write the synthetic lists; return rank()'s {(network id, label): list_id}."""
    conn.execute("TRUNCATE calculated_list_rows, calculated_lists CASCADE")
    out = {}
    for n in range(networks):
        conn.execute("INSERT INTO providers VALUES (%s) ON CONFLICT DO NOTHING", (f"p{n}",))
        product = conn.execute(
            "INSERT INTO products(provider,name) VALUES (%s,'residential') ON CONFLICT "
            "(provider,name) DO UPDATE SET name=excluded.name RETURNING product_id",
            (f"p{n}",),
        ).fetchone()[0]
        for label, hours in WINDOWS.items():
            list_id = conn.execute(
                "INSERT INTO calculated_lists(product_id,cohort,window_hours,content_hash,"
                "entries,figures,created_build) VALUES (%s,'network',%s,'h','{}','{}',1) "
                "RETURNING list_id",
                (product, hours),
            ).fetchone()[0]
            conn.execute(FILL, {"list": list_id, "shift": n * rows // 6, "rows": rows})
            out[f"p{n}:residential:network", label] = list_id
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--networks", type=int, default=18)
    parser.add_argument("--rows", type=int, nargs="+", default=[25_000, 100_000])
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    admin = read_credentials(os.environ["PG_TEST_ADMIN_FILE"], "postgres")
    with psycopg.connect(**admin, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS ips_rank_measure")
        conn.execute("CREATE DATABASE ips_rank_measure")
    path = Path(tempfile.mkdtemp()) / "db.toml"
    database = credentials_file(admin | {"dbname": "ips_rank_measure"}, path)
    storage.initialize(database)
    store.initialize(database)
    with storage.connect(database) as conn:
        for statement in LABELS:
            conn.execute(statement, {"places": max(args.rows) * 3 // 2})
    for rows in args.rows:
        with storage.connect(database) as conn:
            ids = lists(conn, args.networks, rows)
        times = []
        for _ in range(args.rounds):
            with storage.connect(database) as conn:
                start = time.perf_counter()
                netlists.rank(conn, ids)
                conn.commit()
                times.append(time.perf_counter() - start)
                conn.execute("UPDATE calculated_list_rows SET ips_rank=NULL,ips_peers=NULL")
            with psycopg.connect(**read_credentials(database, "postgres"), autocommit=True) as conn:
                conn.execute("VACUUM ANALYZE calculated_list_rows")
        ranked = len(ids) * rows
        print(json.dumps({"rows": ranked, "seconds": statistics.median(times), "all": times}))


if __name__ == "__main__":
    main()
