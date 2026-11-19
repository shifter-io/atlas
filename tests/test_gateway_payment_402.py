"""PAUSE-402-1: a gateway that refuses our account for payment (HTTP 402) is a refusal like a
login's 401 or 407 (proxy_auth, the 402 kept in status_codes), so the PROXY-AUTH-PAUSE-1 path
pauses the network, opens no outage and keeps the runs out of the measurements; the alerts and
the pause say "payment required" (19 Nov 2026: a gateway answered 402 to every request and about
160 false outages opened). A target's own 402 never counts."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from helpers import Gateway, server
from orchestrator_cases import prepare, query, set_setting
from psycopg.types.json import Jsonb
from pulse_cases import evaluated, products, run, tape, window
from test_login_pause import NOW, PAUSES, manager, paused, state
from test_proxy_login import ALERTS, REGIONS

from atlas import JsonSink, ProxyConfig, RunConfig, RunOptions, proxy_login
from atlas import run as measure
from atlas.daily import batches
from atlas.daily import ingest as daily_ingest
from atlas.netpause import passed
from atlas.orchestrator import login_pause
from atlas.storage import connect
from atlas.targets import Assessment, EchoIpTarget, RequestSpec, TargetRegistry

pytest_plugins = ["test_storage", "orchestrator_cases"]


async def refused_402(tmp_path, target="generic_http", url=None, requests=2):
    """A run through a loopback gateway that refuses our account with 402."""
    targets = TargetRegistry()
    targets.register(EchoIpTarget(target, url or "http://ip-info.com/myip"))
    async with server(Gateway(auth=True, refusal=402)) as port:
        proxy = ProxyConfig("http", "127.0.0.1", port, "test", "unpaid")
        return await measure(
            RunConfig(target, proxy, requests=requests, concurrency=25),
            JsonSink(tmp_path / target),
            RunOptions(targets=targets),
        )


@pytest.mark.parametrize(
    "target,url",
    [
        ("generic_http", None),  # plain HTTP: the gateway itself answers the request
        ("generic_https", "https://localhost:9/"),  # HTTPS: the gateway answers the CONNECT
    ],
)
async def test_a_gateway_402_is_a_refusal_counted_as_payment(tmp_path, target, url):
    result = await refused_402(tmp_path, target, url)
    assert result.errors == {"proxy_auth": 2}
    assert result.status_codes == {"402": 2}
    assert (result.proxy_errors, result.blocks) == (2, 0)
    summary = result.to_dict()
    assert proxy_login.summary_refused(summary)  # not judged, not a baseline, not imported
    assert proxy_login.payments(summary["status_codes"]) == 2
    assert not passed(summary)  # a login test answered 402 refuses the resume


async def test_a_site_targets_402_over_plain_http_is_the_targets(tmp_path):
    class Site:
        id, version, engine, pool_eligible = "site", "1", "http", False

        def build_request(self):
            return RequestSpec("http://example.test/checkout")

        def assess(self, status, body):
            return Assessment(error="http_status")

    targets = TargetRegistry()
    targets.register(Site())
    async with server(Gateway(status=402, body=b"pay")) as port:
        result = await measure(
            RunConfig("site", ProxyConfig("http", "127.0.0.1", port), requests=1),
            JsonSink(tmp_path),
            RunOptions(targets=targets),
        )
    assert result.errors == {"http_status": 1}


def test_payment_needs_half_of_the_refused():
    assert proxy_login.payment(50, 100) and proxy_login.payment(100, 100)
    assert not proxy_login.payment(49, 100) and not proxy_login.payment(5, 0)
    assert proxy_login.payments({"402": 7, "407": 3}) == 7
    assert proxy_login.payments(None) == proxy_login.payments({"200": 4}) == 0


async def test_gateway_402_pulses_pause_the_network_as_payment_required(database, db, tmp_path):
    """The incident: every pulse request of the network answered 402 by its gateway, in two
    regions, from the first window. Before PAUSE-402-1 that was a pulse_down and no pause."""
    prepare(database, REGIONS)
    places = manager(tmp_path)
    await set_setting(db, "browser.settings_file", str(places.browser))
    (alpha,) = products(database, "alpha")
    result = await refused_402(tmp_path, requests=100)
    for country in ("DE", "GB"):
        rows = [(window(0), result.completed, result.completed, 300)]
        key = await run(tmp_path, database, "pulse", window(0), alpha, country, rows)
        with connect(database) as conn:
            conn.execute(
                "UPDATE run_buckets SET errors=%s,status_codes=%s,proxy_errors=%s,timeouts=0 "
                "WHERE run_key=%s",
                (Jsonb(result.errors), Jsonb(result.status_codes), result.proxy_errors, key),
            )
    assert (await evaluated(db, 0))["opened"] == 0
    assert tape(database, "pulse_down") == []
    assert query(database, "SELECT count(*) FROM outages") == [(0,)]
    ((severity, _, _, data, message),) = query(database, ALERTS)
    assert severity == "major" and (data["payment"], data["refused"]) == (200, 200)
    assert message.startswith("alpha/residential: payment required. 100% of its pulse requests")
    assert "refused for payment (HTTP 402)" in message
    done = await login_pause.login_pause(db, places.root, NOW)
    assert done == {"paused": ["alpha"], "nothing_to_pause": []}
    assert state(places) == (False, False, False)
    ((severity, acknowledged, data, message),) = query(database, PAUSES)
    assert (severity, acknowledged, data["reason"]) == ("major", False, "payment required")
    assert (data["payment"], data["refused"], data["requests"]) == (200, 200, 200)
    assert message == (
        "alpha: payment required. The gateway refuses our account for payment (HTTP 402 on 200 "
        "of 200 requests since 12:00 UTC). Its tests are paused. Top up or renew the account, "
        "then resume after a one-request login test passes."
    )
    # The resume stays refused while the gateway answers 402.
    login = (await refused_402(tmp_path, "generic_https", "https://localhost:9/", 5)).to_dict()
    with connect(database) as conn:
        refused = login_pause.resume(conn, places, "alpha", lambda *_: login, NOW)
    assert refused["resumed"] is False and refused["login"]["refused"] == 5
    assert state(places) == (False, False, False)


async def test_a_login_pause_still_says_login(database, db, tmp_path):
    """407s (no 402 among them) keep the login wording and reason."""
    _, _, done = await paused(database, db, tmp_path)
    ((_, _, data, message),) = query(database, PAUSES)
    assert done["paused"] == ["alpha"] and data["reason"] == "login refused"
    assert data["payment"] == 0 and "credentials no longer work" in message


async def test_a_daily_test_refused_for_payment_raises_a_payment_trigger(database, db):
    prepare(database, REGIONS)
    (alpha,) = products(database, "alpha")
    hour = SimpleNamespace(db=db, fallback=datetime(2026, 10, 5, 13, tzinfo=UTC))
    countries = [
        batches.Country("DE", {}, login=(100, 100), payment=100),
        batches.Country("GB", {}, login=(40, 50), payment=40),
    ]
    manifest = {"provider": "alpha", "product": "residential"}
    cells = [daily_ingest.Cell("alpha", "alpha", manifest, {}, countries)]
    await daily_ingest._login_triggers(hour, cells)
    rows = query(database, "SELECT data,message FROM alerts WHERE kind='proxy_login_refused'")
    ((data, message),) = rows
    assert (data["product_id"], data["refused"], data["requests"]) == (alpha, 140, 150)
    assert data["payment"] == 140 and data["source"] == "daily"
    assert message == (
        "alpha: 140 of 150 daily test requests of the hour 2026-10-05 13:00 UTC were refused "
        "for payment (HTTP 402)."
    )
