"""Node agent: configuration, uploads and runs against a stub orchestrator.

The in-process census + pulse end-to-end run is in `test_node_e2e.py`.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from helpers import server
from node_cases import RotatingGateway

from atlas.models import ProxyConfig
from atlas.node import NodeAgent, NodeSettings, Rejected, provider_factory, read_token

SHIFTER = Path(__file__).resolve().parents[1] / "configs" / "shifter.toml"


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
        NodeSettings(
            "http://orchestrator",
            "t",
            provider="shifter",
            product="residential",
            region="fra1",
            proxy_factory=factory,
            work_dir=tmp_path,
        )
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
            NodeSettings(
                "http://o",
                "t",
                provider="shifter",
                product="datacenter",
                region="fra1",
                proxy_factory=factory,
                work_dir=tmp_path,
            )
        ).config(assignment())
    with pytest.raises(ValueError, match="--region or --host-name"):
        NodeAgent(
            NodeSettings(
                "http://o", "t", provider="x", product="y", proxy_factory=factory, work_dir=tmp_path
            )
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
        NodeSettings(
            str(test_server.make_url("")),
            "tok",
            provider="p",
            product="q",
            region="fra1",
            proxy_factory=None,
            work_dir=tmp_path,
            upload_backoff=0.01,
        )
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
        NodeSettings(
            str(test_server.make_url("")),
            "tok",
            provider="p",
            product="q",
            region="fra1",
            proxy_factory=None,
            work_dir=tmp_path,
            upload_backoff=0.01,
        )
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text("{}")
    import aiohttp

    from atlas.node import Active

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

    from atlas.node import Active
    from atlas.targets import EchoIpTarget

    async def ok(request, attempt):
        return web.json_response({"ok": True, "status": "done"})

    test_server, calls = await stub_orchestrator(
        {"/v1/progress": ok, "/v1/results": ok, "/v1/failed": ok}
    )
    gateway = RotatingGateway(lambda n: f"10.0.1.{n % 4 + 1}")
    async with server(gateway) as port:
        agent = NodeAgent(
            NodeSettings(
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
