"""PROXY-AUTH-PAUSE-2 (#1413): a network paused for refused credentials gets no verdict that
would end its open outages, so the pause ends them as not measured at its `since` (the first
refused window; 12:00 here), never before their own start, and lists them on the pause; the
undo, `atlas daily reopen-outages`, reopens them and keeps them open."""

import json
from datetime import UTC, datetime, timedelta

from orchestrator_cases import prepare, query, set_setting
from pulse_cases import products
from test_login_pause import NOW, PASSED, PAUSES, manager, refused_pulses
from test_proxy_login import REGIONS

from atlas.cli import main
from atlas.orchestrator import login_pause
from atlas.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]

SINCE = datetime(2026, 10, 5, 12, tzinfo=UTC)
ENDS = "SELECT ended_at,end_reason FROM outages WHERE outage_id=%s"


def outage(database, product, country, started, source="pulse", ended=None):
    """An outage of the product (network-wide for a pulse one: batch_id NULL); its id."""
    with connect(database) as conn:
        return conn.execute(
            "INSERT INTO outages(product_id,country_code,source,started_at,ended_at) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING outage_id",
            (product, country, source, started, ended),
        ).fetchone()[0]


def ends(database, *outages):
    return [query(database, ENDS, (outage_id,))[0] for outage_id in outages]


async def refused(database, db, tmp_path):
    """alpha refused at login in two regions from 12:00, beta measured; nothing paused yet."""
    prepare(database, REGIONS)
    places = manager(tmp_path)
    await set_setting(db, "browser.settings_file", str(places.browser))
    alpha = await refused_pulses(tmp_path, database, db)
    (beta,) = products(database, "beta")
    return places, alpha, beta


async def test_the_pause_ends_the_open_outages_as_not_measured_at_its_since(database, db, tmp_path):
    places, alpha, beta = await refused(database, db, tmp_path)
    before = outage(database, alpha, "DE", SINCE - timedelta(hours=2))  # a real one, ends at 12:00
    partly = outage(database, alpha, "GB", SINCE + timedelta(minutes=20))  # opened while refused
    census = outage(database, alpha, "GB", SINCE - timedelta(hours=1), source="census")
    ended = SINCE - timedelta(hours=2)
    done = outage(database, alpha, "DE", SINCE - timedelta(hours=3), ended=ended)
    other = outage(database, beta, "DE", SINCE - timedelta(hours=2))  # beta is not paused

    outcome = await login_pause.login_pause(db, places.root, NOW)

    assert outcome == {"paused": ["alpha"], "nothing_to_pause": [], "outages_ended": 3}
    assert ends(database, before, partly, census) == [
        (SINCE, "not_measured"),
        (SINCE + timedelta(minutes=20), "not_measured"),  # adds no downtime
        (SINCE, "not_measured"),
    ]
    assert ends(database, done, other) == [(ended, None), (None, None)]
    ((_, _, data, message),) = query(database, PAUSES)
    assert data["closed_outages"] == sorted([before, partly, census])
    assert "open outages end, not measured, at 12:00 UTC" in message


async def test_an_outage_opened_while_paused_ends_at_the_next_tick_until_the_resume(
    database, db, tmp_path
):
    places, alpha, _ = await refused(database, db, tmp_path)
    first = outage(database, alpha, "DE", SINCE - timedelta(hours=1))
    await login_pause.login_pause(db, places.root, NOW)
    late = outage(database, alpha, "GB", NOW + timedelta(minutes=5))  # a pulse of the paused hour
    tick = await login_pause.login_pause(db, places.root, NOW + timedelta(minutes=6))
    assert tick["outages_ended"] == 1
    assert ends(database, late) == [(NOW + timedelta(minutes=5), "not_measured")]
    assert query(database, PAUSES)[0][2]["closed_outages"] == [first, late]
    assert (await login_pause.login_pause(db, places.root, NOW))["outages_ended"] == 0

    with connect(database) as conn:
        assert login_pause.resume(conn, places, "alpha", lambda *_: PASSED, NOW)["posted"]
    after = outage(database, alpha, "DE", NOW + timedelta(minutes=30))  # measured again
    assert (await login_pause.login_pause(db, places.root, NOW))["outages_ended"] == 0
    assert ends(database, after) == [(None, None)]


async def test_the_undo_reopens_the_ended_outages_and_keeps_them_open(database, db, tmp_path):
    places, alpha, _ = await refused(database, db, tmp_path)
    real = outage(database, alpha, "DE", SINCE - timedelta(hours=1))
    taken = outage(database, alpha, "GB", SINCE - timedelta(hours=1))
    await login_pause.login_pause(db, places.root, NOW)
    newer = outage(database, alpha, "GB", NOW + timedelta(minutes=1))  # GB has an open one again
    with connect(database) as conn:
        undone = login_pause.reopen(conn, "alpha")
    assert undone == {"network": "alpha", "paused": True, "reopened": [real], "kept": [taken]}
    assert ends(database, real, taken, newer) == [
        (None, None),
        (SINCE, "not_measured"),
        (None, None),
    ]
    assert query(database, PAUSES)[0][2]["outages_kept"] is True
    # The pause keeps them: its next ticks end none.
    tick = await login_pause.login_pause(db, places.root, NOW + timedelta(minutes=2))
    assert tick["outages_ended"] == 0 and ends(database, real, newer) == [(None, None)] * 2


async def test_the_undo_command_prints_one_line_and_exits_3_without_a_pause(
    database, db, tmp_path, capsys
):
    places, alpha, _ = await refused(database, db, tmp_path)
    ended = outage(database, alpha, "DE", SINCE - timedelta(hours=1))
    await login_pause.login_pause(db, places.root, NOW)
    args = ["daily", "reopen-outages", "--database-file", str(database)]
    assert main([*args, "beta"]) == 3
    assert json.loads(capsys.readouterr().out) == {
        "network": "beta",
        "paused": False,
        "reopened": [],
    }
    assert main([*args, "alpha"]) == 0
    assert json.loads(capsys.readouterr().out)["reopened"] == [ended]
    assert ends(database, ended) == [(None, None)]
