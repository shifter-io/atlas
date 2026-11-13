"""Each run keeps the DB-IP release it was enriched with (DBIP-DAILY-1, review B1).

With a daily DB-IP update, older runs keep an older release. The calculation must use each
run's own release, not only the newest one, or every older run would lose its countries."""

import hashlib
import json
import os
from datetime import datetime, timedelta

import dbip_fakes
import pytest
from archive_job_cases import shift
from calculated_cases import POLICY, OtherIP
from test_storage import enrichment, measurement

from atlas.calculated.calculator import calculate
from atlas.enrichment import enrich_run
from atlas.storage import connect, ingest

pytest_plugins = ["test_storage", "calculated_template"]


@pytest.fixture
def database(calculated_database):
    """Every test here works on the current calculated schema, copied from its template."""
    return calculated_database


US = {"country_code": "US", "asn": 15169, "isp": "Fixture"}
DE = {"country_code": "DE", "asn": 3320, "isp": "Fixture"}


def located(snapshot) -> dict[str, int]:
    """Countries seen per provider in the 24-hour window."""
    return {p["provider"]: p["windows"]["24h"]["pool"]["countries"] for p in snapshot["products"]}


async def test_each_run_keeps_the_release_it_was_enriched_with(database, tmp_path):
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


def end_of(*runs) -> datetime:
    """A cutoff 5 minutes after the last of runs finished."""
    return max(
        datetime.fromisoformat(json.loads((d / "summary.json").read_text())["finished_at"])
        for d in runs
    ) + timedelta(minutes=5)


def countries(snapshot) -> dict[str, list[str]]:
    """The countries of each provider's exit IPs in the 24-hour window (country rows with IPs)."""
    return {
        p["provider"]: sorted(
            r["country_code"] for r in p["windows"]["24h"]["breakdowns"]["country"] if r["ips"]
        )
        for p in snapshot["products"]
    }


def release_ids(database, *sha256s) -> list[int]:
    with connect(database) as conn:
        rows = conn.execute(
            "SELECT release_id FROM dbip_releases WHERE sha256=ANY(%s) ORDER BY release_id",
            (list(sha256s),),
        ).fetchall()
    return [r[0] for r in rows]


def move(database, run, delta) -> None:
    """Move a stored run and its rows by delta."""
    run_id = json.loads((run / "summary.json").read_text())["run_id"]
    with connect(database) as conn:
        row = conn.execute(
            "SELECT run_key,started_at FROM test_runs WHERE run_id=%s", (run_id,)
        ).fetchone()
        shift(conn, row[0], row[1] + delta)


async def test_the_snapshot_lists_every_release_its_runs_used(database, tmp_path):
    with connect(database) as conn:  # recorded by the update before any run used it
        conn.execute("INSERT INTO dbip_releases(sha256,metadata) VALUES (%s,'{}')", ("c" * 64,))
    older = await measurement(tmp_path, requests=3)
    enrichment(older, attributes=US, release="a" * 64)
    ingest(older, database)
    newer = await measurement(tmp_path, requests=3, provider="second", transport_factory=OtherIP)
    enrichment(newer, attributes=DE, release="b" * 64)
    ingest(newer, database)
    # A run without enrichment (a pulse) keeps its IP, unlabelled, and names no release.
    pulse = await measurement(tmp_path, requests=2, provider="third")
    ingest(pulse, database)

    # Only the build's runs count: an hour earlier, the older run alone is in the window, so
    # the build names release a only, not the newer b of a run after its cutoff.
    hour = timedelta(hours=1)
    move(database, older, -hour)
    earlier = calculate(database, POLICY, as_of=end_of(older) - hour)
    assert earlier["dbip_releases"] == release_ids(database, "a" * 64)
    assert earlier["dbip_release"]["sha256"] == "a" * 64
    assert countries(earlier)["fixture"] == ["US"]

    snapshot = calculate(database, POLICY, as_of=end_of(older, newer, pulse))
    assert snapshot["dbip_releases"] == release_ids(database, "a" * 64, "b" * 64)
    assert snapshot["dbip_release"]["sha256"] == "b" * 64
    assert countries(snapshot) == {"fixture": ["US"], "second": ["DE"], "third": []}
    third = next(p for p in snapshot["products"] if p["provider"] == "third")
    assert third["windows"]["24h"]["pool"]["unique_ips"] == 1


async def test_an_ip_in_runs_of_two_releases_takes_its_lowest_run_key(database, tmp_path):
    """Lowest run_key, not the newest or oldest release: release b is recorded first, so its id
    is the lower one. The first network's first run has the newer release (a), the second
    network's first run the older one (b); both first runs say US, the later runs DE."""
    with connect(database) as conn:
        conn.execute("INSERT INTO dbip_releases(sha256,metadata) VALUES (%s,'{}')", ("b" * 64,))
    runs = []
    for provider, transport, release, attributes in (
        ("fixture", None, "a", US),
        ("second", OtherIP, "b", US),
        ("fixture", None, "b", DE),
        ("second", OtherIP, "a", DE),
    ):
        extra = {"transport_factory": transport} if transport else {}
        run = await measurement(tmp_path, requests=2, provider=provider, **extra)
        enrichment(run, attributes=attributes, release=release * 64)
        ingest(run, database)
        runs.append(run)
    assert release_ids(database, "b" * 64) < release_ids(database, "a" * 64)

    snapshot = calculate(database, POLICY, as_of=end_of(*runs))
    assert countries(snapshot) == {"fixture": ["US"], "second": ["US"]}
    assert located(snapshot) == {"fixture": 1, "second": 1}
    assert snapshot["dbip_releases"] == release_ids(database, "a" * 64, "b" * 64)


async def test_a_file_replaced_under_its_name_is_a_new_release(database, tmp_path):
    """The update renames each new file over db-ip/dbip-full.mmdb: the release is the file's
    sha256, so the same name with new content is a new release, and the run enriched with the
    old file keeps its labels for the same IP."""
    mmdb = tmp_path / "db-ip" / "dbip-full.mmdb"
    mmdb.parent.mkdir()
    contents = (
        dbip_fakes.tiny_mmdb(low=dbip_fakes.US, high=dbip_fakes.DE, marker="a"),
        dbip_fakes.tiny_mmdb(low=dbip_fakes.DE, high=dbip_fakes.US, marker="b"),
    )
    runs = []
    for provider, content in zip(("fixture", "second"), contents, strict=True):
        staged = mmdb.with_suffix(".partial")
        staged.write_bytes(content)
        os.replace(staged, mmdb)  # atomically, under the one fixed name
        run = await measurement(tmp_path, requests=2, provider=provider)  # both see one IP
        enrich_run(run, mmdb)
        ingest(run, database)
        runs.append(run)

    sha256s = [hashlib.sha256(c).hexdigest() for c in contents]
    with connect(database) as conn:
        names = conn.execute(
            "SELECT metadata->>'file' FROM dbip_releases ORDER BY release_id"
        ).fetchall()
    assert [r[0] for r in names] == ["dbip-full.mmdb"] * 2
    snapshot = calculate(database, POLICY, as_of=end_of(*runs))
    assert countries(snapshot) == {"fixture": ["US"], "second": ["DE"]}
    assert snapshot["dbip_releases"] == release_ids(database, *sha256s)
    assert snapshot["dbip_release"]["sha256"] == sha256s[1]


async def test_a_build_without_enriched_observations_names_no_release(database, tmp_path):
    """An enriched run after the cutoff is outside the build: with only a pulse run in its
    window, the snapshot names no release."""
    enriched = await measurement(tmp_path, requests=2)
    enrichment(enriched, attributes=US, release="a" * 64)
    ingest(enriched, database)
    pulse = await measurement(tmp_path, requests=2, provider="third")
    ingest(pulse, database)
    move(database, enriched, timedelta(hours=1))

    snapshot = calculate(database, POLICY, as_of=end_of(pulse))
    assert snapshot["dbip_release"] is None and snapshot["dbip_releases"] == []
    assert countries(snapshot)["third"] == []
