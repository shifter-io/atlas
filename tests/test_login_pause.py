"""PROXY-AUTH-PAUSE-1: a network refusing our proxy login (HTTP 407) is paused by the
login_pause job (its pulse fleet entry, daily formula and browser entry), with one Slack alert
per pause, and resumed by `atlas daily resume-network` only after a login test passes. The hour
report's side is in test_daily_verify.py."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from orchestrator_browser_cases import settings_file, settings_text
from orchestrator_cases import prepare, query, set_setting
from pulse_cases import evaluated, products
from test_proxy_login import REGIONS, pulse_run
from test_storage import measurement

from atlas import netpause
from atlas.cli import main
from atlas.daily import batches
from atlas.daily import ingest as daily_ingest
from atlas.models import ProxyConfig
from atlas.orchestrator import login_pause
from atlas.storage import connect, ingest

pytest_plugins = ["test_storage", "orchestrator_cases"]

NOW = datetime(2026, 10, 5, 13, 10, tzinfo=UTC)  # minute 10: files may move
PAUSES = (
    "SELECT severity,acknowledged_at IS NOT NULL,data,message FROM alerts "
    "WHERE kind='network_paused' ORDER BY alert_id"
)
TRIGGERS = "SELECT acknowledged_at IS NOT NULL FROM alerts WHERE kind='proxy_login_refused'"
PASSED = {"completed": 5, "successes": 5, "errors": {}}
REFUSED = {"completed": 5, "successes": 0, "errors": {"proxy_auth": 5}}
COUNTS = {"refused": 90, "requests": 100, "payment": 0}  # a daily test mostly refused at login


def manager(tmp_path, names=("alpha", "beta")):
    """A manager root with both networks in the pulse fleet, the daily formulas and alpha in the
    browser settings (as alpha/residential)."""
    root = tmp_path / "manager"
    (root / "pulse").mkdir(parents=True)
    (root / "daily" / "formulas").mkdir(parents=True)
    networks = {
        name: {"product": "residential", "provider_config": f"c/{name}.toml", "credentials": "s"}
        for name in names
    }
    (root / "pulse" / "fleet.json").write_text(json.dumps({"regions": [], "networks": networks}))
    for name in names:
        (root / "daily" / "formulas" / f"{name}.json").write_text(json.dumps({"network": name}))
    text = settings_text().replace("example-a/residential", "alpha/residential")
    return netpause.Places(root, settings_file(tmp_path, text))


def state(places, name="alpha"):
    """Which of the network's three parts are live: pulses, daily, browser."""
    fleet = json.loads(places.fleet.read_text())["networks"]
    text = places.browser.read_text()
    return (name in fleet, places.formula(name).exists(), f'"{name}/residential"' in text)


async def refused_pulses(tmp_path, database, db, k=0, regions=("DE", "GB")):
    (alpha,) = products(database, "alpha")
    for country in regions:
        await pulse_run(tmp_path, database, alpha, country, k, refused=100)
    await evaluated(db, k)
    return alpha


async def paused(database, db, tmp_path):
    prepare(database, REGIONS)
    places = manager(tmp_path)
    await set_setting(db, "browser.settings_file", str(places.browser))
    alpha = await refused_pulses(tmp_path, database, db)
    return places, alpha, await login_pause.login_pause(db, places.root, NOW)


async def test_407_in_two_regions_pauses_the_three_parts_and_posts_once(database, db, tmp_path):
    places, alpha, done = await paused(database, db, tmp_path)
    assert done == {"paused": ["alpha"], "nothing_to_pause": [], "outages_ended": 0}
    assert state(places) == (False, False, False)
    assert state(places, "beta")[:2] == (True, True)  # beta is not touched
    held = json.loads(places.held.read_text())
    assert held["alpha"]["provider_config"] == "c/alpha.toml"
    assert places.formula("alpha", paused=True).exists()
    assert '"alpha/paused"' in places.browser.read_text()
    (copies,) = (places.root / "archive").iterdir()
    assert copies.name == "alpha-pause-20261005T131000"
    assert {path.name for path in copies.iterdir()} == {
        "pulse-fleet.json",
        "formulas-alpha.json",
        f"{places.browser.parent.name}-browser-pulse.toml",
    }
    ((severity, acknowledged, data, message),) = query(database, PAUSES)
    assert (severity, acknowledged, data["reason"], data["product_id"]) == (
        "major",
        False,
        "login refused",
        alpha,
    )
    assert data["paused"] == ["pulses", "daily", "browser"] and data["problems"] == []
    assert message == (
        "alpha: the account credentials no longer work, or the account is refused the IP-check "
        "host (HTTP 401, 403 or 407 on 200 of 200 requests since 12:00 UTC). Its tests are paused "
        "and its open outages end, not measured, at 12:00 UTC. Resume after a one-request login "
        "test passes."
    )
    assert query(database, TRIGGERS) == [(True,)]  # the pause stands for the trigger


async def test_below_the_share_nothing_is_paused(database, db, tmp_path):
    prepare(database, REGIONS)
    places = manager(tmp_path)
    await set_setting(db, "browser.settings_file", str(places.browser))
    await refused_pulses(tmp_path, database, db, regions=("DE",))  # one region: no trigger
    assert await login_pause.login_pause(db, places.root, NOW) == {
        "paused": [],
        "nothing_to_pause": [],
        "outages_ended": 0,
    }
    assert state(places) == (True, True, True)
    assert query(database, PAUSES) == []


async def test_an_already_paused_network_posts_nothing_more(database, db, tmp_path):
    places, _, _ = await paused(database, db, tmp_path)
    await refused_pulses(tmp_path, database, db, k=12)  # the next hour: a new trigger row
    assert await login_pause.login_pause(db, places.root, NOW + timedelta(hours=1)) == {
        "paused": [],
        "nothing_to_pause": [],
        "outages_ended": 0,
    }
    assert len(query(database, PAUSES)) == 1
    assert query(database, TRIGGERS) == [(True,), (True,)]


async def test_a_mostly_refused_browser_run_triggers_and_a_network_without_parts_waits(
    database, db, tmp_path
):
    from census_pulse_cases import batch, batch_run

    prepare(database, REGIONS)
    places = manager(tmp_path, names=("beta",))  # alpha has no parts on this manager
    (alpha,) = products(database, "alpha")
    key = ingest(await measurement(tmp_path / "browser-run"), database)["run_key"]
    with connect(database) as conn:
        conn.execute(
            "UPDATE test_runs SET summary=jsonb_set(summary,'{errors}','{\"proxy_auth\":9}') "
            "WHERE run_key=%s",
            (key,),
        )
        made = batch(conn, "browser", NOW - timedelta(minutes=30), target="x_web")
        batch_run(conn, made, alpha, NOW - timedelta(minutes=20), run_key=key, requests=10)
    done = await login_pause.login_pause(db, places.root, NOW)
    assert done == {"paused": [], "nothing_to_pause": [alpha], "outages_ended": 0}
    assert query(database, PAUSES) == []


async def test_nothing_moves_while_the_hours_read_the_files(db, tmp_path):
    quiet = NOW.replace(minute=56)
    assert "skipped" in await login_pause.login_pause(db, tmp_path, quiet)
    with pytest.raises(netpause.PauseError):
        login_pause.resume(None, netpause.Places(tmp_path), "alpha", now=quiet)


async def test_resume_refuses_on_407_and_restores_the_three_parts_after_a_passing_test(
    database, db, tmp_path
):
    places, _, _ = await paused(database, db, tmp_path)
    with connect(database) as conn:
        refused = login_pause.resume(conn, places, "alpha", lambda *_: REFUSED, NOW)
    assert refused["resumed"] is False and refused["login"]["refused"] == 5
    assert state(places) == (False, False, False)
    with connect(database) as conn:
        done = login_pause.resume(conn, places, "alpha", lambda *_: PASSED, NOW)
    assert done["resumed"] is True and done["restored"] == ["pulses", "daily", "browser"]
    assert state(places) == (True, True, True)
    assert '"alpha/paused"' not in places.browser.read_text()
    assert json.loads(places.held.read_text()) == {}
    assert query(database, PAUSES)[0][1] is True  # the pause is acknowledged
    resumed = "SELECT message,acknowledged_at IS NOT NULL FROM alerts WHERE kind='network_resumed'"
    ((message, acknowledged),) = query(database, resumed)
    assert acknowledged and message.startswith("alpha: the login test passed (5 of 5 requests")
    # The old triggers are done with: the next tick pauses nothing.
    assert (await login_pause.login_pause(db, places.root, NOW))["paused"] == []


async def test_the_resume_command_prints_one_line_and_exits_3_while_refused(
    database, db, tmp_path, monkeypatch, capsys
):
    places, _, _ = await paused(database, db, tmp_path)
    monkeypatch.setattr(netpause, "quiet", lambda now: False)
    monkeypatch.setattr(netpause, "login_test", lambda *_: REFUSED)
    args = ["daily", "resume-network", "alpha", "--database-file", str(database)]
    assert main([*args, "--root", str(places.root)]) == 3
    assert json.loads(capsys.readouterr().out)["resumed"] is False
    monkeypatch.setattr(netpause, "login_test", lambda *_: PASSED)
    assert main([*args, "--root", str(places.root)]) == 0
    assert state(places) == (True, True, True)


def test_the_login_test_uses_the_pulse_config_and_credentials(tmp_path, monkeypatch):
    import atlas.providers
    import atlas.runner

    places, seen = manager(tmp_path), {}

    def provider(path, credentials_file):
        seen["files"] = (path, credentials_file)
        return ProxyConfig.from_url("http://user:secret@192.0.2.1:8080")

    async def run(config, sink):
        seen["config"] = config
        return SimpleNamespace(to_dict=lambda: PASSED)

    monkeypatch.setattr(atlas.providers, "load_provider", provider)
    monkeypatch.setattr(atlas.runner, "run", run)
    assert netpause.login_test(places, "alpha") == PASSED
    pulse = places.root / "pulse"
    assert seen["files"] == (pulse / "c/alpha.toml", pulse / "s")
    config = seen["config"]
    assert (config.target, config.requests, config.processes) == ("generic_https", 5, 1)
    with pytest.raises(netpause.PauseError):
        netpause.entry(places, "gamma")
    assert netpause.passed(PASSED) and not netpause.passed(REFUSED)
    assert not netpause.passed({"successes": 0})


def test_a_browser_edit_the_settings_refuse_changes_nothing_and_is_reported(tmp_path):
    places = manager(tmp_path)
    before = places.browser.read_text()
    places.browser.write_text(before.replace('"alpha/residential"', '"alpha/residential", "x"'))
    edited = places.browser.read_text()
    held, problems = netpause.pause(places, "alpha", "alpha/residential", NOW)
    assert held == ["pulses", "daily"]
    assert problems and problems[0].startswith("browser settings refused the edit")
    assert places.browser.read_text() == edited
    assert not list(places.browser.parent.glob(".*.partial"))


async def test_a_daily_test_mostly_refused_raises_the_trigger(database, db):
    prepare(database, REGIONS)
    alpha, _beta = products(database, "alpha", "beta")
    hour = SimpleNamespace(db=db, fallback=datetime(2026, 10, 5, 13, tzinfo=UTC))

    def cell(network, *logins):
        countries = [batches.Country(f"C{i}", {}, login=login) for i, login in enumerate(logins)]
        manifest = {"provider": network, "product": "residential"}
        return daily_ingest.Cell(network, network, manifest, {}, countries)

    cells = [cell("alpha", (90, 100), (0, 50)), cell("alpha", (10, 10)), cell("beta", (40, 100))]
    await daily_ingest._login_triggers(hour, cells)
    rows = query(database, "SELECT severity,data FROM alerts WHERE kind='proxy_login_refused'")
    ((severity, data),) = rows  # alpha: 100 of 160 refused; beta: 40 of 100, below the share
    assert severity == "minor"
    assert (data["product_id"], data["refused"], data["requests"]) == (alpha, 100, 160)
    assert data["since"] == "2026-10-05T13:00:00+00:00" and data["source"] == "daily"


async def test_a_trigger_from_before_the_resume_does_not_pause_again(database, db, tmp_path):
    """The runs of the hour of the pause go on: hour 13's daily import, after the resume, still
    raises a trigger dated 13:00. Only a refusal after the resume pauses again."""
    places, alpha, _ = await paused(database, db, tmp_path)
    with connect(database) as conn:
        assert login_pause.resume(conn, places, "alpha", lambda *_: PASSED, NOW)["posted"]
    network = {"provider": "alpha", "product": "residential"}
    await login_pause.daily_refused(db, network, COUNTS, NOW.replace(minute=0))
    assert (await login_pause.login_pause(db, places.root, NOW))["paused"] == []
    assert state(places) == (True, True, True) and len(query(database, PAUSES)) == 1
    later = datetime.now(UTC) + timedelta(hours=1)  # a daily test of an hour after the resume
    await login_pause.daily_refused(db, network, COUNTS, later)
    again = await login_pause.login_pause(db, places.root, NOW + timedelta(minutes=1))
    assert again["paused"] == ["alpha"] and len(query(database, PAUSES)) == 2


async def test_resuming_a_network_that_is_not_paused_posts_nothing(database, db, tmp_path):
    prepare(database, REGIONS)
    products(database, "alpha")
    places = manager(tmp_path)
    with connect(database) as conn:
        done = login_pause.resume(conn, places, "alpha", lambda *_: PASSED, NOW)
    assert (done["resumed"], done["posted"]) == (True, False)
    assert query(database, "SELECT count(*) FROM alerts") == [(0,)]
