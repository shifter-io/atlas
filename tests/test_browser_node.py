"""The browser host process: networks on one pool, per-target settings, refusals, logs. The
browser-node command (--check and serve) is in test_browser_node_cli.py."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from browser_node_cases import (
    CHEAP,
    CONFIGS,
    SHIFTER,
    TOKENS,
    Fleet,
    Orchestrator,
    browser_work,
    host_view,
    log_lines,
    make_state,
    private,
    serve_until,
)

from atlas import RequestContext
from atlas.browser import node as host_module
from atlas.browser import run as run_module
from atlas.browser.node import (
    BrowserHost,
    RunTiming,
    bound_registry,
    load_networks,
    slot_deadline,
    try_seconds,
    untargeted_factory,
)
from atlas.browser.settings import load_settings
from atlas.browser.transport import NullRecorder
from atlas.errors import UserError
from atlas.node import Rejected, provider_factory


async def test_two_networks_share_one_pool_with_per_target_settings(tmp_path, monkeypatch):
    # The deadline 10 minutes after the slot end, so that a run started now always has time,
    # even in a slot's last 2 minutes (the real margin: test_deadline_is_the_slot_end...).
    monkeypatch.setattr(run_module, "DEADLINE_MARGIN_S", -600)
    state = make_state(tmp_path)
    past = datetime.now(UTC) - timedelta(hours=2)
    google = browser_work(2, "google_search")
    google_start = google["start_at"]
    work = {
        TOKENS[SHIFTER]: [
            browser_work(1, "amazon_pdp"),
            google,
            browser_work(4, "amazon_pdp", kind="pulse"),
            browser_work(6, "amazon_pdp", start=past),
        ],
        TOKENS[CHEAP]: [browser_work(3, "amazon_pdp"), browser_work(5, "cloudflare_challenge")],
    }
    fake = Orchestrator(work)
    server = TestServer(fake.app)
    await server.start_server()
    fleet = Fleet()
    host = BrowserHost(
        host_view(state),
        state,
        str(server.make_url("")),
        launch=fleet.launch,
        poll_interval=0.02,
        heartbeat_interval=0.05,
    )
    try:
        await serve_until(host, fake, lambda: len(fake.results) == 4 and len(fake.failed) == 2)
    finally:
        await server.close()

    # Each network registers and uploads with its own token, as region browser and its host.
    assert sorted(token for token, _ in fake.registered) == sorted(TOKENS.values())
    assert {body["host"] for _, body in fake.registered} == {"browser-1"}
    assert {body["region"] for _, body in fake.registered} == {"browser"}
    owner = {batch: token for batch, (token, _) in fake.results.items()}
    assert owner == {1: TOKENS[SHIFTER], 2: TOKENS[SHIFTER], 3: TOKENS[CHEAP], 6: TOKENS[SHIFTER]}
    assert fake.failed == {
        4: (TOKENS[SHIFTER], "node error (UnexpectedKind)"),
        5: (TOKENS[CHEAP], "node error (UnknownTarget)"),
    }
    summaries = {batch: summary for batch, (_, summary) in fake.results.items()}
    # Tries, concurrency and timeout come from the settings per target, not the assignment.
    for batch, tries, timeout in ((1, 3, 20.0), (2, 2, 15.0), (3, 3, 20.0)):
        summary = summaries[batch]
        assert summary["requested"] == tries and summary["completed"] == tries
        assert summary["config"]["concurrency"] == 3
        assert summary["config"]["timeout_seconds"] == timeout
        assert summary["config"]["runner_location"] == "browser"
    # Untargeted (D11): no country requested or sent; the targeted network sends US.
    assert summaries[1]["config"]["requested_country"] == "us"
    assert summaries[3]["config"]["requested_country"] is None
    assert summaries[3]["config"]["provider_config"]["parameters"] == {}
    cheap = [p for p in fleet.proxies() if p.username.startswith("cheapacct")]
    shifter = [p for p in fleet.proxies() if not p.username.startswith("cheapacct")]
    assert len(cheap) == 3 and len(shifter) == 5
    assert all("country" not in p.username + p.password for p in cheap)
    assert all("country-us" in p.username for p in shifter)
    # A deadline in the past starts no try.
    assert summaries[6]["started"] == 0 and summaries[6]["stop_reason"] == "deadline"
    # One pool for both networks: runs overlapped, never more browsers than the pool's size.
    assert 2 <= fleet.peak <= 3 and len(fleet.browsers) <= 3
    assert fleet.closed == len(fleet.browsers)  # shutdown closed every browser

    lines = {line["batch"]: line for line in log_lines(state) if line["kind"] == "run"}
    assert set(lines) == {1, 2, 3, 6}
    starts = {line["batch"]: line for line in log_lines(state) if line["kind"] == "start"}
    assert set(starts) == set(lines)  # the mini's watchdog reads starts too (#1257)
    assert starts[1]["network"] == SHIFTER and starts[1]["target"] == "amazon_pdp"
    assert starts[1]["at"] <= lines[1]["at"]
    first = lines[1]
    assert first["network"] == SHIFTER and first["target"] == "amazon_pdp"
    assert first["tries_requested"] == 3 and first["tries_done"] == 3
    assert first["ok"] >= 0 and first["latency_ms"]["p50"] is not None
    assert first["latency_ms"]["p90"] >= first["latency_ms"]["p50"]
    assert first["browser_seconds"] > 0 and first["queue_wait_ms_p50"] is not None
    assert first["finished_before_deadline"] is True and first["geo_targeting"] is True
    assert lines[3]["geo_targeting"] is False
    expected = slot_deadline(google_start, 30)
    assert datetime.fromisoformat(lines[2]["deadline"]).timestamp() == expected
    assert lines[6]["tries_done"] == 0 and lines[6]["finished_before_deadline"] is False


def test_deadline_is_the_slot_end_minus_two_minutes():
    assert (
        slot_deadline("2026-11-04T10:17:30+00:00", 60)
        == datetime(2026, 11, 4, 10, 58, tzinfo=UTC).timestamp()
    )
    assert (
        slot_deadline("2026-11-04T10:31:00+00:00", 30)
        == datetime(2026, 11, 4, 10, 58, tzinfo=UTC).timestamp()
    )
    assert (
        slot_deadline("2026-11-04T10:00:00+00:00", 5)
        == datetime(2026, 11, 4, 10, 3, tzinfo=UTC).timestamp()
    )


def test_targets_are_bound_to_their_settings_waits(tmp_path):
    settings = host_view(make_state(tmp_path))
    bound = bound_registry(settings)
    overview = bound.get("ai_overview")
    assert overview.answer_wait_s == 40 and overview.page_timeout_s == 20
    assert try_seconds(overview, 20) == 40.0  # the wait is cut to the page timeout
    assert try_seconds(bound.get("amazon_pdp"), 20) == 20.0
    example = load_settings(CONFIGS / "browser-pulse.example.toml").host("browser-1")
    challenge = bound_registry(example).get("cloudflare_challenge")
    assert challenge.challenge_wait_s == 20  # the class default is 30
    assert try_seconds(challenge, 45) == 65.0
    chatgpt = bound_registry(example).get("ai_chatgpt")
    assert chatgpt.answer_wait_s == 60 and chatgpt.page_timeout_s == 45
    with pytest.raises(ValueError):
        bound.get("linkedin_public")  # not in the settings, so not in the host registry


def replace_with_directory(path: Path) -> None:
    path.unlink()
    path.mkdir(mode=0o700)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: (d / "node.token").unlink(), "node.token is missing"),
        (lambda d: (d / "node.token").chmod(0o644), "node.token must be owned"),
        (lambda d: (d / "credentials.toml").chmod(0o640), "credentials.toml must be owned"),
        (lambda d: private(d / "node.token", "two words"), "unusable network files"),
        (lambda d: private(d / "provider.toml", "schema_version = 9"), "unusable network"),
        # BP-HOST-FIXES-1 review M1: read when the factory is made, not only when it builds.
        (lambda d: private(d / "provider.toml", "provider = ["), r"\(TOMLDecodeError\)"),
        (lambda d: private(d / "provider.toml", 'provider = "nope"'), r"\(ValueError\)"),
        (lambda d: replace_with_directory(d / "provider.toml"), "must be a regular file"),
    ],
)
def test_bad_network_files_are_refused_at_start(tmp_path, change, message):
    state = make_state(tmp_path)
    change(state / "networks" / "proxy-cheap.residential")
    with pytest.raises(UserError, match=message):
        load_networks(state, host_view(state))


def test_a_linked_token_is_refused(tmp_path):
    state = make_state(tmp_path)
    directory = state / "networks" / "shifter.residential"
    shutil.move(directory / "node.token", tmp_path / "token")
    (directory / "node.token").symlink_to(tmp_path / "token")
    with pytest.raises(UserError, match="node.token"):
        load_networks(state, host_view(state))


GEONODE_ROTATING = (CONFIGS / "geonode.toml").read_text().replace("port = 10000", "port = 9000")


@pytest.mark.parametrize("factory", [provider_factory, untargeted_factory])
@pytest.mark.parametrize(
    ("provider", "config", "policy"),
    [
        ("proxy-seller", (CONFIGS / "proxy-seller.toml").read_text(), "per_request"),
        ("shifter", (CONFIGS / "shifter.toml").read_text(), "rotate"),
        ("geonode", GEONODE_ROTATING, "rotate"),
    ],
)
def test_networks_get_a_new_exit_per_request_in_their_grammars_policy(
    tmp_path, factory, provider, config, policy
):
    # Proxy-Seller rotates by port and refuses "rotate"; the others keep it.
    private(tmp_path / "provider.toml", config)
    private(tmp_path / "credentials.toml", f'[{provider}]\naccount = "acct"\npassword = "pw"\n')
    proxies = factory(tmp_path / "provider.toml", tmp_path / "credentials.toml")("US")
    assert proxies.session_policy == policy


@pytest.mark.parametrize("factory", [provider_factory, untargeted_factory])
def test_geonode_from_mains_file_gets_a_new_exit_per_request_on_port_9000(
    tmp_path, factory, caplog
):
    """BP-HOST-FIXES-1: main's configs/geonode.toml names the sticky port 10000, where the
    grammar refuses "rotate"; both factories use the rotating port 9000 instead (logged once)
    and leave the file as it is."""
    config = (CONFIGS / "geonode.toml").read_text()
    assert "port = 10000" in config
    private(tmp_path / "provider.toml", config)
    private(tmp_path / "credentials.toml", '[geonode]\naccount = "acct"\npassword = "pw"\n')
    caplog.set_level(logging.INFO)
    build = factory(tmp_path / "provider.toml", tmp_path / "credentials.toml")
    proxies, again = build("US"), build("GB")
    assert (proxies.host, proxies.port) == ("proxy.geonode.io", 9000)
    assert proxies.session_policy == "rotate"
    assert again.port == 9000 and again.session_policy == "rotate"
    users = {proxies.resolve(RequestContext("run", 0, n)).username for n in range(3)}
    assert len(users) == 1 and "session" not in users.pop()  # the gateway picks each exit
    assert ("country" in proxies.parameters) is (factory is provider_factory)
    assert caplog.text.count("geonode: a new exit per request uses rotating gateway port") == 1
    assert (tmp_path / "provider.toml").read_text() == config


def test_run_timing_counts_tries_latency_queue_and_browser_time():
    class Inner:
        seen = 0

        def try_done(self, record, screenshot):
            Inner.seen += 1

    timing = RunTiming(Inner())
    ok = {"navigate": 100, "settle": 20, "queue": 5, "context": 3, "total": 140}
    refused = dict.fromkeys(ok) | {"total": 1}
    timing.try_done({"timings_ms": ok, "error": None}, None)
    timing.try_done({"timings_ms": ok | {"navigate": 300}, "error": "timeout"}, None)
    timing.try_done({"timings_ms": refused, "error": "proxy_connect"}, None)
    assert (timing.tries, timing.ok, Inner.seen) == (3, 1, 3)
    assert timing.latency == [120, 320] and timing.queue == [5, 5]
    assert timing.busy_ms == 270
    assert host_module._percentile([], 0.5) is None
    assert host_module._percentile([5, 1, 9, 3], 0.5) == 3
    assert host_module._percentile([5, 1, 9, 3], 0.9) == 9
    NullRecorder().try_done({}, None)


async def test_the_hourly_line_reports_utilization_pool_and_memory(tmp_path):
    state = make_state(tmp_path)
    fleet = Fleet()
    host = BrowserHost(host_view(state), state, "http://orchestrator.invalid", launch=fleet.launch)
    host.hour_interval = 10.0
    host.busy_ms = 6000
    line = await host.hour_line()
    assert line["kind"] == "hour" and line["browsers"] == 3
    assert line["browser_seconds"] == 6 and line["utilization"] == 0.2
    assert set(line["pool"]) >= {"leases", "launches", "crashes", "resident_bytes"}
    assert "rss_mb" in line and host.busy_ms == 0
    assert {k: line[k] for k in ("outbox_pending", "outbox_bytes", "rejected", "conflicts")} == {
        "outbox_pending": 0, "outbox_bytes": 0, "rejected": 0, "conflicts": 0
    }  # fmt: skip
    assert line["confirmed_last_hour"] == 0 and "incomplete" not in line
    assert line["discarded"] == 0 and line["unsealed"] == 0 and line["discard_failed"] == 0
    host.log.close()
    [written] = log_lines(state)
    assert written == json.loads(json.dumps(line))


async def test_the_hourly_loop_writes_lines_until_stopped(tmp_path):
    state = make_state(tmp_path)
    host = BrowserHost(
        host_view(state), state, "http://orchestrator.invalid", launch=Fleet().launch
    )
    host.hour_interval = 0.02
    # Stopped once two lines are written, not after a sleep: a loaded runner wrote one in 0.1 s.
    lines, two = [], asyncio.Event()
    hour_line = host.hour_line

    async def counted():
        lines.append(await hour_line())
        if len(lines) == 2:
            two.set()
        return lines[-1]

    host.hour_line = counted
    stop = asyncio.Event()
    task = asyncio.create_task(host._hourly(stop))
    await asyncio.wait_for(two.wait(), 30)
    stop.set()
    await asyncio.wait_for(task, 30)
    host.log.close()
    assert len(log_lines(state)) == len(lines) >= 2


async def test_a_network_that_cannot_register_stops_the_host(tmp_path):
    state = make_state(tmp_path)

    async def refuse(request):
        return web.json_response({"error": "bad token"}, status=401)

    app = web.Application()
    app.router.add_route("*", "/v1/register", refuse)
    server = TestServer(app)
    await server.start_server()
    host = BrowserHost(host_view(state), state, str(server.make_url("")), launch=Fleet().launch)
    try:
        with pytest.raises(Rejected):
            await host.run(asyncio.Event())
    finally:
        await server.close()
