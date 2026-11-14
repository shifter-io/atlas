"""PULSE-ENRICH-1: pulse uploads are stored with their IP rows, enriched on the manager with its
current DB-IP release in place of the droplet's (or with the droplet's when the manager has no
file), and the enrichment is deterministic, so a retried upload imports the same artifact."""

import hashlib
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from dbip_fakes import DE, US, tiny_mmdb
from orchestrator_cases import planned_run, query
from test_storage import enrichment, measurement

from atlas.enrichment import MMDB_ENV, SharedEnricher, Unavailable
from atlas.orchestrator import pulse_enrich
from atlas.orchestrator.app import build_app
from atlas.orchestrator.archive import pack

pytest_plugins = ["test_storage", "orchestrator_cases"]

DROPLET = "b" * 64  # the release in the droplet's image


def mmdb_file(path, low=US, high=DE, marker="a"):
    """A tiny DB-IP file (8.8.8.8 is in its low half) and its release (sha256)."""
    path.write_bytes(tiny_mmdb(low, high, marker=marker))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
async def orchestrator(database, db, monkeypatch):
    """Start an orchestrator whose DBIP_MMDB names the file given (None: unset); its client."""
    clients = []

    async def start(mmdb):
        monkeypatch.delenv(MMDB_ENV, raising=False)
        if mmdb is not None:
            monkeypatch.setenv(MMDB_ENV, str(mmdb))
        clients.append(TestClient(TestServer(build_app(database, with_scheduler=False, db=db))))
        await clients[-1].start_server()
        return clients[-1]

    yield start
    for client in clients:
        await client.close()


async def upload_pulse(database, db, client, tmp_path):
    """Upload a pulse run enriched by its droplet, as the node does; the batch id."""
    batch_id, headers = await planned_run(database, db, client)
    query(database, "UPDATE test_batches SET kind='pulse' RETURNING batch_id")
    directory = await measurement(tmp_path / "pulse", requests=3)
    enrichment(directory, release=DROPLET)
    upload = pack(directory, tmp_path / "run.tar.gz").read_bytes()
    for _ in range(2):  # a retry after a lost response is acknowledged, not imported again
        reply = await client.post(f"/v1/results?batch_id={batch_id}", data=upload, headers=headers)
        assert reply.status == 200, await reply.text()
    return batch_id


def stored(database):
    """Each run's observations and the releases it was enriched with."""
    return query(
        database,
        "SELECT (SELECT count(*) FROM ip_observations o WHERE o.run_key=t.run_key),"
        "array(SELECT d.sha256 FROM run_enrichments e JOIN dbip_releases d USING(release_id) "
        "WHERE e.run_key=t.run_key) FROM test_runs t",
    )


async def test_a_pulse_upload_is_stored_with_its_ips_and_the_manager_s_release(
    database, db, orchestrator, tmp_path
):
    path, release = mmdb_file(tmp_path / "manager.mmdb", low=DE, high=US)
    await upload_pulse(database, db, await orchestrator(path), tmp_path)
    ((observations, releases),) = stored(database)
    assert observations == 1 and releases == [release]  # the droplet's release is not imported
    ((counts,),) = query(database, "SELECT country_counts FROM run_enrichments")
    assert counts == {"DE": {"requests": 3, "unique_ips": 1}}  # labelled by the manager's file
    assert query(database, "SELECT count(*) FROM product_ips") == [(1,)]


async def test_without_a_manager_file_the_droplet_s_enrichment_is_imported(
    database, db, orchestrator, tmp_path
):
    await upload_pulse(database, db, await orchestrator(None), tmp_path)
    assert stored(database) == [(1, [DROPLET])]
    assert query(database, "SELECT count(*) FROM alerts") == [(0,)]


async def test_an_unreadable_manager_file_keeps_the_droplet_s_and_alerts(
    database, db, orchestrator, tmp_path
):
    client = await orchestrator(tmp_path / "missing.mmdb")
    batch_id = await upload_pulse(database, db, client, tmp_path)
    assert stored(database) == [(1, [DROPLET])]
    ((kind, severity, data),) = query(database, "SELECT kind,severity,data FROM alerts")
    assert (kind, severity) == ("dbip_unavailable", "major")
    assert data == {"batch_id": batch_id, "error": "FileNotFoundError"}


async def test_reenrich_replaces_the_droplet_s_and_gives_the_same_bytes_again(tmp_path):
    path, release = mmdb_file(tmp_path / "a.mmdb")
    directory = await measurement(tmp_path / "run")
    enrichment(directory, release=DROPLET)
    shared = SharedEnricher(path)
    assert shared.reenrich(directory)["rows"] == 1
    artifact = directory / f"enriched-{release}.jsonl.gz"
    names = sorted(p.name for p in directory.glob("enrich*"))
    assert names == [artifact.name, f"enrichment-{release}.json"]
    sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    shared.reenrich(directory)  # an upload retried: the same artifact, so the same checksum
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == sha
    # The daily update renames a new release over the file: the next run uses it.
    _, second = mmdb_file(tmp_path / "new.mmdb", low=DE, high=US, marker="b")
    (tmp_path / "new.mmdb").replace(path)
    assert shared.reenrich(directory)["release"]["sha256"] == second != release
    assert [p.name for p in directory.glob("enrichment-*")] == [f"enrichment-{second}.json"]
    shared.close()
    shared.close()  # closing twice is harmless


async def test_an_unopenable_file_is_unavailable_and_touches_no_run(tmp_path):
    directory = await measurement(tmp_path / "run")
    enrichment(directory, release=DROPLET)
    broken = tmp_path / "broken.mmdb"
    broken.write_bytes(b"not a database")
    for path in (tmp_path / "missing.mmdb", broken):
        with pytest.raises(Unavailable):
            SharedEnricher(path).reenrich(directory)
    assert (directory / f"enrichment-{DROPLET}.json").exists()


async def test_a_run_enrichment_refuses_goes_on_to_the_import(tmp_path, caplog, db):
    path, _ = mmdb_file(tmp_path / "a.mmdb")
    directory = await measurement(tmp_path / "run")
    summary = json.loads((directory / "summary.json").read_text())
    (directory / "ips.jsonl.gz").write_bytes(b"changed")  # its checksum no longer matches
    await pulse_enrich.enrich(db, SharedEnricher(path), 7, directory, summary)
    assert "batch 7 not enriched here (ValueError)" in caplog.text
    unpooled = summary | {"pool_eligible": False}
    await pulse_enrich.enrich(db, SharedEnricher(path), 8, directory, unpooled)
    await pulse_enrich.enrich(db, None, 9, directory, summary)
    assert not list(directory.glob("enrich*"))  # none wrote an enrichment
