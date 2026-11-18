"""Browser bundle ingest (orchestrator/browser_ingest.py, K-BP7): POST /v1/results for kind
browser, through PostgreSQL, ARCHIVE-1's fake S3 bucket and the aiohttp test client. The accepted
and refused bundles; concurrency and run states are in test_browser_ingest_runs.py."""

import json
from datetime import timedelta

import pytest
from browser_ingest_cases import (
    KEY,
    MEMBERS,
    SLOT,
    assert_nothing_kept,
    browser_run,
    make_bundle,
    run_rows,
    set_settings,
    sha256,
    upload,
)
from orchestrator_cases import bearer, planned_run, query
from test_storage import measurement

from atlas.browser import contracts as bc
from atlas.orchestrator import browser_ingest
from atlas.orchestrator.archive import pack
from atlas.orchestrator.nodes import issue_token

# The fixtures (db, client, scratch, world) are browser_ingest_cases'.
pytest_plugins = ["test_storage", "browser_ingest_cases"]


# --- the happy path ---------------------------------------------------------------------------


async def test_a_bundle_is_archived_verified_ingested_and_nothing_is_kept(
    database, client, world, scratch
):
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert status == 200, body
    assert body["ingested"] is True and body["status"] == "done"
    assert body["archived"] == {"key": KEY, "sha256": sha256(data)}
    assert bc.confirmed((status, body), sha256(data))
    stored, meta, acl = world["bucket"].objects["/" + KEY]
    assert stored == data and acl == "private"
    meta = {k.lower(): v for k, v in meta}
    assert meta["x-amz-meta-sha256"] == sha256(data)
    assert meta["x-amz-meta-run-id"] == run_id and meta["x-amz-meta-batch-id"] == str(batch_id)
    ((run_status, run_key, buckets),) = run_rows(database, batch_id)
    assert (run_status, run_key) == ("done", body["run_key"]) and buckets >= 1
    assert query(database, "SELECT run_id::text FROM test_runs") == [(run_id,)]
    assert query(database, "SELECT status FROM test_batches WHERE batch_id=%s", (batch_id,)) == [
        ("done",)
    ]
    assert_nothing_kept(world, scratch)


async def test_an_untargeted_run_is_archived_under_untargeted(database, client, world, scratch):
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id, requested=None)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert status == 200, body
    key = "browser-files/alpha.res/amazon_pdp/untargeted/2026-10-01T1200Z.tar.gz"
    assert body["archived"]["key"] == key and "/" + key in world["bucket"].objects


async def test_the_local_archive_directory_replaces_spaces(database, db, client, world, scratch):
    archive_dir = world["tmp"] / "staging-archive"
    await set_settings(db, {"browser.archive_dir": str(archive_dir), "archive.spaces_file": None})
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert status == 200, body
    assert (archive_dir / KEY).read_bytes() == data and world["bucket"].objects == {}
    assert json.loads((archive_dir / f"{KEY}.meta.json").read_text()) == {
        "batch-id": str(batch_id),
        "run-id": run_id,
    }
    assert_nothing_kept(world, scratch)


async def test_without_an_archive_a_bundle_is_refused_503(database, db, client, world, scratch):
    await set_settings(db, {"archive.spaces_file": None})
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert (status, body) == (503, {"error": "archive_not_configured"})
    assert run_rows(database, batch_id)[0][1] is None


# --- checksums --------------------------------------------------------------------------------


async def test_a_wrong_checksum_is_refused_and_nothing_is_kept(database, client, world, scratch):
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id)
    token = world["tokens"]["alpha"]
    status, body, _ = await upload(client, token, batch_id, data, run_id, sha="0" * 64)
    assert (status, body) == (400, {"error": bc.ERROR_CHECKSUM_MISMATCH})
    assert world["bucket"].objects == {} and query(database, "SELECT * FROM test_runs") == []
    assert_nothing_kept(world, scratch)
    # The header is required for a browser bundle, and must be a SHA-256.
    reply = await client.post(
        f"/v1/results?batch_id={batch_id}",
        data=data,
        headers=bearer(token) | {bc.HEADER_BUNDLE_ID: run_id},
    )
    assert reply.status == 400 and "64 hex" in (await reply.json())["error"]
    status, body, _ = await upload(client, token, batch_id, data, run_id, sha="xyz")
    assert status == 400 and "64 hex" in body["error"]
    # The mini retries after re-hashing: the same bundle goes through.
    assert (await upload(client, token, batch_id, data, run_id))[0] == 200


async def test_other_kinds_verify_a_checksum_when_sent(database, db, client, tmp_path):
    batch_id, headers = await planned_run(database, db, client)
    directory = await measurement(tmp_path, requests=3)
    data = pack(directory, tmp_path / "run.tar.gz").read_bytes()
    url = f"/v1/results?batch_id={batch_id}"
    wrong = await client.post(url, data=data, headers=headers | {bc.HEADER_SHA256: "f" * 64})
    assert wrong.status == 400 and (await wrong.json())["error"] == bc.ERROR_CHECKSUM_MISMATCH
    assert query(database, "SELECT * FROM test_runs") == []
    right = await client.post(url, data=data, headers=headers | {bc.HEADER_SHA256: sha256(data)})
    assert right.status == 200 and (await right.json())["ingested"] is True


# --- refusals ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"manifest": {"run_id": "00000000-0000-4000-8000-000000000000"}}, "differ"),
        ({"manifest": {"batch_id": 999999}}, "another batch"),
        ({"manifest": {"target": "google_search"}}, "another target"),
        ({"manifest": {"requested_country": None, "geo_targeting": False}}, "geo targeting"),
        ({"manifest": {"format": "other"}}, "manifest"),
        ({"manifest": {"network": {"provider": "beta", "product": "res"}}}, "another provider"),
        ({"no_manifest": True}, "no browser/manifest.json"),
        ({"extra": [("browser/screenshots/0000.jpg", b"jpeg")]}, "files differ"),
        ({"engine": "chrome", "config_engine": "camoufox"}, "engine disagrees"),
        ({"version": 1, "config_engine": "chrome"}, "engine disagrees"),
    ],
)
async def test_a_bundle_that_does_not_match_its_run_is_refused_400(
    database, client, world, scratch, options, message
):
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id, **options)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert status == 400 and message in body["error"], body
    assert world["bucket"].objects == {} and query(database, "SELECT * FROM test_runs") == []
    assert query(database, "SELECT kind FROM alerts") == [("results_rejected",)]
    assert run_rows(database, batch_id)[0][0] == "failed"
    assert_nothing_kept(world, scratch)


async def test_a_bundle_declaring_rows_past_the_bound_is_refused_400(
    database, db, client, world, scratch
):
    """SEC-FOLLOWUP-3: results.max_artifact_rows bounds a bundle's declared rows too."""
    await set_settings(db, {"results.max_artifact_rows": 0})
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert status == 400 and "rows are outside the allowed range" in body["error"], body
    assert world["bucket"].objects == {} and query(database, "SELECT * FROM test_runs") == []
    assert run_rows(database, batch_id)[0][0] == "failed"
    assert_nothing_kept(world, scratch)


async def test_a_replay_reads_no_rows_so_the_row_bound_does_not_apply(
    database, db, client, world, scratch
):
    batch_id = browser_run(database, "alpha")
    args = (world["tokens"]["alpha"], batch_id, *await make_bundle(world["tmp"], batch_id))
    status, body, _ = await upload(client, *args)
    assert status == 200, body
    await set_settings(db, {"results.max_artifact_rows": 0})
    replay_status, replay, _ = await upload(client, *args)
    assert replay_status == 200 and replay == body
    assert_nothing_kept(world, scratch)


# --- manifest versions and engines ---------------------------------------------------------


def scopes(database):
    """{run id: (scope id, the scope's browser_engine)} of every stored run."""
    rows = query(
        database,
        "SELECT r.run_id::text,s.scope_id,s.scope->'config'->>'browser_engine' "
        "FROM test_runs r JOIN scope_details s USING(scope_id)",
    )
    return {run_id: (scope_id, engine) for run_id, scope_id, engine in rows}


async def test_a_version_1_bundle_still_ingests(database, client, world, scratch):
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id, version=1)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert status == 200, body
    assert body["ingested"] is True and body["archived"]["key"] == KEY
    assert [engine for _, engine in scopes(database).values()] == [None]
    assert_nothing_kept(world, scratch)


async def test_a_chrome_bundle_ingests_into_its_own_scope(database, client, world, scratch):
    engines = {}
    for hour, engine in enumerate(("camoufox", "chrome", "camoufox")):
        slot = SLOT + timedelta(hours=hour)
        batch_id = browser_run(database, "alpha", slot=slot)
        data, run_id = await make_bundle(world["tmp"], batch_id, engine=engine, slot=slot)
        status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
        assert status == 200, body
        engines[run_id] = engine
    found = scopes(database)
    assert {run_id: engine for run_id, (_, engine) in found.items()} == engines
    by_engine = {
        engine: {s for r, (s, e) in found.items() if e == engine} for engine in engines.values()
    }
    # The two Camoufox runs share a scope; the Chrome run has its own.
    assert len(by_engine["camoufox"]) == len(by_engine["chrome"]) == 1
    assert by_engine["camoufox"] != by_engine["chrome"]
    assert_nothing_kept(world, scratch)


async def test_a_listed_file_with_other_bytes_and_a_missing_bundle_id_are_refused(
    database, client, world, scratch
):
    batch_id = browser_run(database, "alpha")
    entry = {"name": MEMBERS.tries, "bytes": 1, "sha256": "d" * 64}
    data, run_id = await make_bundle(world["tmp"], batch_id, manifest={"files": [entry]})
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert status == 400 and "differs from its manifest entry" in body["error"]
    other = browser_run(database, "alpha", target="google_search")
    data, _ = await make_bundle(world["tmp"], other, target="google_search")
    status, body, _ = await upload(client, world["tokens"]["alpha"], other, data, "")
    assert status == 400 and bc.HEADER_BUNDLE_ID in body["error"]


# --- authentication (D2) ----------------------------------------------------------------------


async def test_a_browser_token_from_another_host_is_refused(database, client, world, scratch):
    token = world["tokens"]["alpha"]
    body = {"provider": "alpha", "product": "res", "region": "browser", "host": "browser-2"}
    reply = await client.post("/v1/register", json=body, headers=bearer(token))
    assert reply.status == 403 and "another host" in (await reply.json())["error"]
    assert query(database, "SELECT host FROM nodes WHERE provider='alpha'") == [("browser-1",)]
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id, host="browser-2")
    status, body, _ = await upload(client, token, batch_id, data, run_id)
    assert (status, body) == (403, {"error": "token belongs to another host"})
    assert world["bucket"].objects == {}


async def test_a_browser_node_must_register_before_it_uploads(database, client, world, scratch):
    node = issue_token(
        database, provider="delta", product="res", region="browser", host="browser-4"
    )
    batch_id = browser_run(database, "delta")
    data, run_id = await make_bundle(world["tmp"], batch_id, "delta", host="browser-4")
    status, body, _ = await upload(client, node["token"], batch_id, data, run_id)
    assert status == 403 and "register" in body["error"]


async def test_a_bad_prefix_or_an_unreachable_database_answers_503(
    database, db, client, world, scratch, monkeypatch
):
    import psycopg

    batch_id = browser_run(database, "alpha")
    args = (world["tokens"]["alpha"], batch_id, *await make_bundle(world["tmp"], batch_id))
    await set_settings(db, {"browser.archive_prefix": "raw"})  # ARCHIVE-1's top level
    status, body, _ = await upload(client, *args)
    assert status == 503 and body["error"].startswith("archive_not_configured: prefix")
    await set_settings(db, {"browser.archive_prefix": "browser-files"})

    async def unreachable(*a, **k):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(browser_ingest, "_settings", unreachable)
    assert (await upload(client, *args))[:2] == (503, {"error": "database unavailable; retry"})
    assert world["bucket"].objects == {}
    assert_nothing_kept(world, scratch)
