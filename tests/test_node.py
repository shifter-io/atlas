"""Node agent: configuration, uploads and the in-process census + pulse end-to-end run."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from helpers import Gateway, server
from test_orchestrator import prepare, query

from proxy_index import __version__
from proxy_index.models import ProxyConfig
from proxy_index.node import NodeAgent, Rejected, provider_factory, read_token
from proxy_index.orchestrator import batches
from proxy_index.orchestrator.app import build_app
from proxy_index.orchestrator.db import Database, put_setting
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

SHIFTER = Path(__file__).resolve().parents[1] / "configs" / "shifter.toml"
BERLIN = {
    "matched": True,
    "country_code": "DE",
    "country": "Germany",
    "region_code": "BE",
    "region": "Berlin",
    "city": "Berlin",
    "latitude": 52.52,
    "longitude": 13.4,
    "isp": "Telekom",
    "organization": None,
    "asn": 3320,
    "asn_organization": "DTAG",
    "connection_type": "Cable/DSL",
    "usage_type": "residential",
}


class GeoEnricher:
    release = {
        "file": "fixture.mmdb",
        "sha256": "cd" * 32,
        "database_type": "DBIP-Full",
        "build_epoch": 1000,
        "built_at": "1970-01-01T00:16:40+00:00",
    }

    def lookup(self, address):
        return dict(BERLIN)


class RotatingGateway(Gateway):
    """Answers every proxied request with the next exit address from `pool`."""

    def __init__(self, pool):
        super().__init__()
        self.pool = pool
        self.served = 0

    async def respond(self, writer):
        self.body = f"{self.pool(self.served)}\n".encode()
        self.served += 1
        await super().respond(writer)


def assignment(**overrides):
    return {
        "batch_id": 7,
        "kind": "pulse",
        "product_id": 1,
        "provider": "shifter",
        "product": "residential",
        "country_code": "DE",
        "region": "fra1",
        "target_id": "generic_https",
        "target_url": "https://ip-info.com/myip",
        "timeout_ms": 9000,
        "concurrency": 25,
        "stop_rule": "count",
        "requests": 100,
        "min_sample": None,
        "start_at": datetime.now(UTC).isoformat(),
    } | overrides


def test_read_token_requires_a_private_file(tmp_path):
    path = tmp_path / "token"
    path.write_text("abc123\n")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="mode 600"):
        read_token(path)
    path.chmod(0o600)
    assert read_token(path) == "abc123"
    path.write_text("two words")
    with pytest.raises(ValueError, match="one token"):
        read_token(path)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_token(link)


def test_config_targets_the_country_with_rotating_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFTER_ACCOUNT", "acct")
    monkeypatch.setenv("SHIFTER_PASSWORD", "secret")
    factory = provider_factory(SHIFTER)
    agent = NodeAgent(
        "http://orchestrator",
        "t",
        provider="shifter",
        product="residential",
        region="fra1",
        proxy_factory=factory,
        work_dir=tmp_path,
    )
    config = agent.config(assignment())
    assert config.requested_country == "de" and config.runner_location == "fra1"
    assert config.target_url is None  # the registry's own URL is not an override
    assert config.timeout_seconds == 9 and config.requests == 100 and config.stop_rule == "count"
    public = config.public()["provider_config"]
    assert public["session_policy"] == "rotate" and public["parameters"] == {"country": "de"}
    proxy = config.proxy.resolve(None)
    assert "country-de" in proxy.username and "sid-" not in proxy.username
    assert "secret" not in repr(config)

    census = agent.config(
        assignment(
            kind="census",
            target_id="generic_http",
            requests=None,
            stop_rule="unique_ratio",
            min_sample=50,
            concurrency=1000,
            target_url="http://127.0.0.1:9/ip",
        )
    )
    assert census.requests is None and census.min_sample == 50
    assert census.target_url == "http://127.0.0.1:9/ip"
    with pytest.raises(ValueError, match="label conflicts"):
        NodeAgent(
            "http://o",
            "t",
            provider="shifter",
            product="datacenter",
            region="fra1",
            proxy_factory=factory,
            work_dir=tmp_path,
        ).config(assignment())
    with pytest.raises(ValueError, match="--region or --host-name"):
        NodeAgent(
            "http://o", "t", provider="x", product="y", proxy_factory=factory, work_dir=tmp_path
        )


async def stub_orchestrator(handlers):
    app = web.Application(client_max_size=64 << 20)
    calls = []

    def route(path):
        async def handle(request):
            body = await request.read()
            calls.append((path, request.headers.get("Authorization"), body))
            return await handlers[path](request, len([c for c in calls if c[0] == path]))

        return handle

    for path in handlers:
        app.router.add_route("*", path, route(path))
    test_server = TestServer(app)
    await test_server.start_server()
    return test_server, calls


async def test_upload_retries_transient_failures_and_cleans_up(tmp_path, monkeypatch):
    async def results(request, attempt):
        if attempt < 3:
            return web.json_response({"error": "busy"}, status=503)
        return web.json_response({"ok": True, "status": "done"})

    test_server, calls = await stub_orchestrator({"/v1/results": results})
    agent = NodeAgent(
        str(test_server.make_url("")),
        "tok",
        provider="p",
        product="q",
        region="fra1",
        proxy_factory=None,
        work_dir=tmp_path,
        upload_backoff=0.01,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text("{}")
    (run_dir / "ips.jsonl.gz").write_bytes(os.urandom(3 << 20))
    import io
    import tarfile

    import aiohttp

    # The archive is streamed from its file, never read whole into memory.
    def forbidden(self):
        raise AssertionError("read_bytes")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    async with aiohttp.ClientSession() as agent.session:
        assert (await agent.upload(5, run_dir))["status"] == "done"
    assert len(calls) == 3 and all(c[1] == "Bearer tok" for c in calls)
    with tarfile.open(fileobj=io.BytesIO(calls[-1][2]), mode="r:gz") as archive:
        assert sorted(archive.getnames()) == ["ips.jsonl.gz", "summary.json"]
        assert archive.getmember("ips.jsonl.gz").size == 3 << 20
    assert not run_dir.exists()
    await test_server.close()


async def test_rejections_are_final_and_shutdown_reports_failure(tmp_path):
    async def results(request, attempt):
        return web.json_response({"error": "nope"}, status=422)

    async def failed(request, attempt):
        return web.json_response({"ok": True, "status": "failed"})

    test_server, calls = await stub_orchestrator({"/v1/results": results, "/v1/failed": failed})
    agent = NodeAgent(
        str(test_server.make_url("")),
        "tok",
        provider="p",
        product="q",
        region="fra1",
        proxy_factory=None,
        work_dir=tmp_path,
        upload_backoff=0.01,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text("{}")
    import aiohttp

    from proxy_index.node import Active

    async with aiohttp.ClientSession() as agent.session:
        with pytest.raises(Rejected):
            await agent.upload(5, run_dir)
        assert run_dir.exists()  # kept for inspection
        # Stopping the node while a run waits for start_at leaves it to be reissued.
        active = Active(assignment(start_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat()))
        agent.active[7] = active
        task = asyncio.create_task(agent.execute(active))
        await asyncio.sleep(0.05)
        active.stop.set()
        await task
    assert [c[0] for c in calls] == ["/v1/results"]
    assert agent.active == {}
    await test_server.close()


async def test_a_target_known_only_to_the_database_runs_as_an_echo_target(tmp_path):
    """Pulse plans every pulse_enabled row, so a row added in the database must run too."""
    import io
    import json
    import tarfile

    import aiohttp

    from proxy_index.node import Active
    from proxy_index.targets import EchoIpTarget

    async def ok(request, attempt):
        return web.json_response({"ok": True, "status": "done"})

    test_server, calls = await stub_orchestrator(
        {"/v1/progress": ok, "/v1/results": ok, "/v1/failed": ok}
    )
    gateway = RotatingGateway(lambda n: f"10.0.1.{n % 4 + 1}")
    async with server(gateway) as port:
        agent = NodeAgent(
            str(test_server.make_url("")),
            "tok",
            provider="fixture",
            product="alpha",
            region="fra1",
            proxy_factory=lambda cc: ProxyConfig("http", "127.0.0.1", port),
            work_dir=tmp_path,
            progress_interval=0.05,
            upload_backoff=0.01,
        )
        work = assignment(
            target_id="generic_http_b",
            target_url="http://127.0.0.1:9/ip",
            requests=6,
            concurrency=2,
        )
        target = agent.targets(work).get("generic_http_b")
        assert target == EchoIpTarget("generic_http_b", "http://127.0.0.1:9/ip", True)
        config = agent.config(work)
        assert config.target == "generic_http_b" and config.target_url is None
        async with aiohttp.ClientSession() as agent.session:
            await agent.execute(Active(work))
        # Only a URL of a scheme an echo target speaks can stand in for an unknown ID.
        with pytest.raises(ValueError, match="Unknown target ID"):
            agent.config(assignment(target_id="generic_ftp", target_url="ftp://127.0.0.1/"))
    assert [c[0] for c in calls if c[0] != "/v1/progress"] == ["/v1/results"]
    with tarfile.open(fileobj=io.BytesIO(calls[-1][2]), mode="r:gz") as archive:
        summary = json.load(archive.extractfile("summary.json"))
    assert summary["target"]["id"] == "generic_http_b"
    assert summary["target"]["url"] == "http://127.0.0.1:9/ip"
    assert summary["successes"] == 6 and summary["unique_ips"] == 4
    await test_server.close()


async def wait_for(predicate, limit=60.0):
    deadline = asyncio.get_running_loop().time() + limit
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.1)


@pytest.fixture
async def orchestrator(database):
    db = Database(database)
    test_server = TestServer(build_app(database, with_scheduler=False, db=db))
    await test_server.start_server()
    yield db, str(test_server.make_url(""))
    await test_server.close()
    await db.close()


def agents_for(base, tokens, port, tmp_path, **options):
    return [
        NodeAgent(
            base,
            token["token"],
            provider="fixture",
            product=token["product"],
            region="fra1",
            proxy_factory=lambda cc: ProxyConfig("http", "127.0.0.1", port),
            work_dir=tmp_path / token["product"],
            enricher=GeoEnricher(),
            poll_interval=0.1,
            heartbeat_interval=0.2,
            progress_interval=0.05,
            upload_backoff=0.05,
            **options,
        )
        for token in tokens
    ]


async def test_census_and_pulse_end_to_end(database, orchestrator, tmp_path):
    db, base = orchestrator
    now = datetime.now(UTC)
    prepare(database, ("DE",), DE=(now + timedelta(seconds=1)).time())
    with connect(database) as conn:
        conn.execute(
            "UPDATE targets SET census_concurrency=4,pulse_requests=12,pulse_concurrency=3,"
            "pulse_enabled=(target_id='generic_http')"
        )
    async with db.connection() as conn:
        for key, value in (
            ("census.lead_seconds", 1),
            ("pulse.lead_seconds", 1),
            ("census.min_sample", 30),
            ("load.delay_seconds", 1),
        ):
            await put_setting(conn, key, value)
    tokens = [
        issue_token(database, provider="fixture", product=product, region="fra1")
        for product in ("alpha", "beta")
    ]
    # Ten exits: the census stops once successes reach 30 > 2 x 10 unique addresses.
    gateway = RotatingGateway(lambda n: f"10.0.0.{n % 10 + 1}")
    async with server(gateway) as port:
        assert (await batches.census_plan(db, now))["created"] == 1
        assert (await batches.pulse_plan(db, now))["created"] == 1
        stop = asyncio.Event()
        agents = agents_for(base, tokens, port, tmp_path)
        tasks = [asyncio.create_task(agent.run(stop)) for agent in agents]
        try:
            await wait_for(
                lambda: (
                    query(
                        database,
                        "SELECT count(*) FROM test_batches WHERE status IN ('scheduled','running')",
                    )[0][0]
                    == 0
                )
            )
        finally:
            stop.set()
            await asyncio.gather(*tasks)

    batches_ = query(database, "SELECT kind,status,start_at FROM test_batches ORDER BY kind")
    # The pulse slot of the census target adds the load wave in fra1's reference country (DE).
    assert [b[:2] for b in batches_] == [("census", "done"), ("load", "done"), ("pulse", "done")]
    runs = query(
        database,
        "SELECT b.kind,p.name,r.status,r.stop_reason,r.requests,r.successes,r.unique_ips,"
        "r.started_at>=b.start_at,r.run_key,r.bytes_down>0 FROM batch_runs r "
        "JOIN test_batches b USING(batch_id) JOIN products p USING(product_id) ORDER BY 1,2",
    )
    census = [run for run in runs if run[0] == "census"]
    pulse = [run for run in runs if run[0] == "pulse"]
    load = [run for run in runs if run[0] == "load"]
    # census_concurrency requests at once, counted like a pulse.
    assert [(r[1], r[2], r[3], r[4]) for r in load] == [
        ("alpha", "done", "requests", 4),
        ("beta", "done", "requests", 4),
    ]
    assert [(r[1], r[2], r[3], r[6], r[7]) for r in census] == [
        ("alpha", "done", "unique_ratio", 10, True),
        ("beta", "done", "unique_ratio", 10, True),
    ]
    # Stopped at the first success past the 30-success sample; in-flight requests finish.
    assert all(30 <= r[5] <= 33 and r[4] == r[5] for r in census)
    assert [(r[1], r[2], r[3], r[4], r[7]) for r in pulse] == [
        ("alpha", "done", "requests", 12, True),
        ("beta", "done", "requests", 12, True),
    ]
    census_keys = sorted(r[8] for r in census)
    geo = query(
        database,
        "SELECT g.run_key,sum(g.completed),min(l.city),min(n.asn) FROM run_geo_buckets g "
        "JOIN enrichment_locations l USING(location_id) JOIN enrichment_networks n "
        "USING(network_id) WHERE g.run_key=ANY(%s) GROUP BY 1 ORDER BY 1",
        (census_keys,),
    )
    assert [(row[0], row[2], row[3]) for row in geo] == [(k, "Berlin", 3320) for k in census_keys]
    assert [row[1] for row in geo] == [r[4] for r in sorted(census, key=lambda r: r[8])]
    linked = query(database, "SELECT count(*) FROM test_runs WHERE batch_id IS NOT NULL")
    assert linked == [(6,)]
    # Both products started the same census at the same scheduled instant.
    started = query(
        database,
        "SELECT r.started_at FROM batch_runs r JOIN test_batches b "
        "USING(batch_id) WHERE b.kind='census'",
    )
    assert abs((started[0][0] - started[1][0]).total_seconds()) < 1.0
    events = query(
        database,
        "SELECT e.kind,p.name,e.country_code,e.signal,e.data "
        "FROM test_events e JOIN products p USING(product_id) "
        "ORDER BY e.kind,p.name",
    )
    assert [e[:4] for e in events] == [
        ("census_finished", "alpha", "DE", 1),
        ("census_finished", "beta", "DE", 1),
        ("census_started", "alpha", "DE", 0),
        ("census_started", "beta", "DE", 0),
    ]
    for _, _, _, _, data in events[:2]:
        assert data["unique_ips"] == 10 and data["region"] == "fra1"
        assert data["stop_reason"] == "unique_ratio" and data["status"] == "done"
        assert data["bytes_down"] > 0 and data["duration_s"] > 0
    assert query(database, "SELECT count(*) FROM test_events") == [(4,)]
    nodes = query(database, "SELECT status,version,rss_mb IS NOT NULL FROM nodes")
    assert nodes == [("online", __version__, True)] * 2
    # Uploaded directories are gone.
    assert os.listdir(tmp_path / "alpha") == os.listdir(tmp_path / "beta") == []


async def test_operator_kill_stops_and_discards_the_run(database, orchestrator, tmp_path):
    db, base = orchestrator
    now = datetime.now(UTC)
    prepare(database, ("DE",), DE=now.time())
    with connect(database) as conn:
        conn.execute("UPDATE targets SET census_concurrency=2,pulse_enabled=false")
    async with db.connection() as conn:
        await put_setting(conn, "census.lead_seconds", 0)
    token = issue_token(database, provider="fixture", product="alpha", region="fra1")
    # Every exit is new, so the census would never stop by itself.
    gateway = RotatingGateway(lambda n: f"10.{n // 65536 % 256}.{n // 256 % 256}.{n % 256}")
    async with server(gateway) as port:
        await batches.census_plan(db, now)
        stop = asyncio.Event()
        (agent,) = agents_for(base, [token], port, tmp_path)
        task = asyncio.create_task(agent.run(stop))
        try:
            # Kill only once a progress post has recorded requests for the run.
            await wait_for(
                lambda: (
                    gateway.served > 20
                    and query(database, "SELECT requests>0 FROM batch_runs") == [(True,)]
                )
            )
            with connect(database) as conn:
                conn.execute("UPDATE test_batches SET status='killed',finished_at=now()")
                conn.execute("UPDATE batch_runs SET status='killed',finished_at=now()")
            await wait_for(lambda: not agent.active)
        finally:
            stop.set()
            await task
    assert query(database, "SELECT count(*) FROM test_runs") == [(0,)]
    assert query(database, "SELECT status,requests>0 FROM batch_runs") == [("killed", True)]
    assert os.listdir(tmp_path / "alpha") == []
