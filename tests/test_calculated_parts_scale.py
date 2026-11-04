"""Regression demonstration (SNAP-SIZE-3a): a synthetic full-size build of 18 networks x 249
countries can be stored.

Skipped unless PROXY_INDEX_SCALE_DEMO=1: it builds about 460 MB of payload JSON. Each of the
4,482 daily product groups is a real calculated group padded to GROUP_BYTES, and the runs are 30
days of daily runs (the audit's hourly build is 1.1-1.6 GB, about 250-350 KB a group; 70 KB
keeps the whole payload under PostgreSQL's 1 GB message limit, so the refusal shown is the jsonb
container limit, which the groups alone pass). Before calculated v8 the
calculation stored the slimmed payload as one jsonb row, which PostgreSQL refuses ("total size
of jsonb array elements exceeds the maximum of 268435455 bytes"); from v8 on (store.write) it is
a head and parts, each part reads back as written and the head stays under parts.HEAD_BYTES.
The test prints the stored sizes (run pytest with -s).
"""

import json
import os
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb
from test_storage import measurement

from proxy_index.calculated import store
from proxy_index.calculated.calculator import calculate
from proxy_index.calculated.policy import digest, load
from proxy_index.storage import connect, ingest

pytest_plugins = ["test_storage"]

POLICY = Path("configs/methodology.toml")
NETWORKS, COUNTRIES, DAYS = 18, 249, 30
GROUP_BYTES = 70_000

pytestmark = pytest.mark.skipif(
    os.environ.get("PROXY_INDEX_SCALE_DEMO") != "1", reason="set PROXY_INDEX_SCALE_DEMO=1"
)


def synthetic(payload):
    """A payload of NETWORKS x COUNTRIES daily groups shaped like payload's first group, with a
    fleet entry per cohort and window, every peer overlap and DAYS days of daily runs."""
    group = payload["products"][0]
    padding = "x" * max(0, GROUP_BYTES - len(json.dumps(group)))
    groups, fleet, overlap = [], [], []
    for c in range(COUNTRIES):
        cohort = f"synthetic{c:03d}"
        for n in range(NETWORKS):
            product_id = 1000 + n
            groups.append(
                group
                | {"id": f"{product_id}:{cohort}", "product_id": product_id, "cohort": cohort}
                | {"provider": f"network{n:02d}", "padding": padding}
            )
        for entry in payload["fleet"][:3]:
            fleet.append(entry | {"cohort": cohort})
        for label in ("24h", "7d", "30d"):
            for a in range(NETWORKS):
                for b in range(a + 1, NETWORKS):
                    overlap.append(
                        {"cohort": cohort, "window": label, "product_a": 1000 + a}
                        | {"product_b": 1000 + b, "shared_ips": 7, "ips_a": 90, "ips_b": 80}
                    )
    run = payload["runs"][0]
    runs = [run | {"run_id": str(uuid.UUID(int=i))} for i in range(NETWORKS * COUNTRIES * DAYS)]
    return payload | {"products": groups, "fleet": fleet, "overlap": overlap, "runs": runs}


@pytest.mark.asyncio
async def test_a_full_size_build_is_stored(database, tmp_path):
    store.initialize(database)
    directory = await measurement(tmp_path, requests=10)
    ingest(directory, database)
    finished = json.loads((directory / "summary.json").read_text())["finished_at"]
    payload = calculate(
        database, POLICY, as_of=datetime.fromisoformat(finished) + timedelta(minutes=5)
    )
    big = synthetic(payload)
    total = sum(len(json.dumps(g)) for g in big["products"])
    assert 268_435_455 < total < 1_000_000_000
    with connect(database) as conn:
        build_id = conn.execute(
            "INSERT INTO calculation_builds(methodology_id,as_of,input_hash) "
            "VALUES (%s,%s,%s) RETURNING build_id",
            (payload["methodology"]["id"], payload["as_of"], "synthetic"),
        ).fetchone()[0]
        big["snapshot_id"] = str(build_id)
        write = getattr(store, "write", None)
        if write is None:  # calculated v7: the slimmed payload in one jsonb row
            conn.execute(
                "INSERT INTO calculated_snapshots VALUES (%s,%s,%s)",
                (build_id, digest(big), Jsonb(big)),
            )
        else:
            # No typed rows: every window names list 0 (payload v3, SNAP-LISTREF-1).
            unlisted = lambda conn, payload: {  # noqa: E731
                (p["id"], label): 0 for p in payload["products"] for label in p["windows"]
            }
            write(conn, build_id, big, load(POLICY)[0], unlisted)
    with connect(database) as conn:
        sizes = conn.execute(
            "SELECT octet_length(payload::text),pg_column_size(payload) FROM calculated_snapshots "
            "WHERE build_id=%s",
            (build_id,),
        ).fetchone()
        rows = conn.execute(
            "SELECT kind,count(*),max(octet_length(part::text)),sum(pg_column_size(part)) "
            "FROM calculated_snapshot_parts WHERE build_id=%s GROUP BY kind ORDER BY kind",
            (build_id,),
        ).fetchall()
        groups = 0
        for position, part in enumerate(store.parts.stream(conn, build_id, "products")):
            group = big["products"][position]
            assert part == getattr(store, "refer", lambda g, _: g)(group, defaultdict(int))
            groups += 1
    print(json.dumps({"head_bytes": sizes, "parts": rows, "group_json_bytes": total}))
    assert groups == NETWORKS * COUNTRIES
    assert dict((kind, count) for kind, count, *_ in rows) == {
        "product": NETWORKS * COUNTRIES,
        "runs": -(-NETWORKS * COUNTRIES * DAYS // store.parts.RUNS_PER_PART),
    }
    assert sizes[0] < store.parts.HEAD_BYTES
    assert store.read_parts(database, str(build_id), "runs")[-1] == big["runs"][-1]
