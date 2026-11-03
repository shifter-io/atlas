"""Each run keeps the DB-IP release it was enriched with (DBIP-DAILY-1, review B1).

With a daily DB-IP update, older runs keep an older release. The calculation must use each
run's own release, not only the newest one, or every older run would lose its countries."""

import json
from datetime import datetime, timedelta

from test_calculated import POLICY, OtherIP
from test_storage import enrichment, measurement

from proxy_index.calculated import store
from proxy_index.calculated.calculator import calculate
from proxy_index.storage import connect, ingest

pytest_plugins = ["test_storage"]

US = {"country_code": "US", "asn": 15169, "isp": "Fixture"}
DE = {"country_code": "DE", "asn": 3320, "isp": "Fixture"}


def located(snapshot) -> dict[str, int]:
    """Countries seen per provider in the 24-hour window."""
    return {p["provider"]: p["windows"]["24h"]["pool"]["countries"] for p in snapshot["products"]}


async def test_each_run_keeps_the_release_it_was_enriched_with(database, tmp_path):
    store.initialize(database)
    older = await measurement(tmp_path, requests=3)
    enrichment(older, attributes=US, release="a" * 64)
    ingest(older, database)
    newer = await measurement(tmp_path, requests=3, provider="second", transport_factory=OtherIP)
    enrichment(newer, attributes=DE, release="b" * 64)
    ingest(newer, database)
    finished = max(
        datetime.fromisoformat(json.loads((d / "summary.json").read_text())["finished_at"])
        for d in (older, newer)
    )
    end = finished + timedelta(minutes=5)

    snapshot = calculate(database, POLICY, as_of=end)
    # The older run keeps release a: its country is still there beside the newer run's.
    assert located(snapshot) == {"fixture": 1, "second": 1}
    assert snapshot["dbip_release"]["sha256"] == "b" * 64

    # A release no run was enriched with yet (the update records it first) changes nothing.
    with connect(database) as conn:
        conn.execute("INSERT INTO dbip_releases(sha256,metadata) VALUES (%s,'{}')", ("c" * 64,))
    later = calculate(database, POLICY, as_of=end)
    assert located(later) == {"fixture": 1, "second": 1}
    assert later["dbip_release"]["sha256"] == "b" * 64
