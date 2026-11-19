"""PROXY-AUTH-ALERT-1: a network whose pulse requests are mostly refused at login (HTTP 407)
raises one proxy_login_refused alert per hour and opens no network outage; the alert is
acknowledged once the refused share drops. detect's side and the daily test are in
test_proxy_login_runs.py."""

from orchestrator_cases import prepare, query
from pulse_cases import W0, evaluated, products, run, tape, window

from atlas import proxy_login
from atlas.orchestrator import pulse_login
from atlas.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]

REGIONS = ("CA", "DE", "GB", "NL", "US")  # tor1, fra1, lon1, ams3, nyc3
ALERTS = (
    "SELECT severity,occurrences,acknowledged_at IS NOT NULL,data,message FROM alerts "
    "WHERE kind='proxy_login_refused' ORDER BY alert_id"
)


async def pulse_run(tmp_path, database, product, country, k, refused, other=0, target=None):
    """A 100-request pulse run of the network in window k: `refused` requests refused at login
    (407) and `other` failed with another answer (a gateway's 503)."""
    rows = [(window(k), 100, refused + other, 300)]
    target = target or "generic_http"
    key = await run(tmp_path, database, "pulse", window(k), product, country, rows, target)
    with connect(database) as conn:
        conn.execute(
            "UPDATE run_buckets SET errors=jsonb_build_object('proxy_auth',%s::int,"
            "'http_status',%s::int),status_codes=jsonb_build_object('407',%s::int,'503',%s::int),"
            "proxy_errors=%s,timeouts=0 WHERE run_key=%s",
            (refused, other, refused, other, refused + other, key),
        )


def test_the_rule_needs_half_of_the_requests_refused():
    assert proxy_login.refused(50, 100) and proxy_login.refused(100, 100)
    assert not proxy_login.refused(49, 100)
    assert not proxy_login.refused(0, 0) and not proxy_login.refused(None, None)
    assert proxy_login.summary_refused({"completed": 10, "errors": {"proxy_auth": 6}})
    assert not proxy_login.summary_refused({"completed": 10, "errors": {"timeout": 9}})
    assert not proxy_login.summary_refused({})


async def test_a_network_refused_in_every_region_raises_one_alert_and_opens_no_outage(
    database, db, tmp_path
):
    prepare(database, REGIONS)
    (alpha,) = products(database, "alpha")
    for k in (0, 1, 2):
        for country in REGIONS:
            await pulse_run(tmp_path, database, alpha, country, k, refused=100)
    assert (await evaluated(db, 0))["opened"] == 0
    assert (await evaluated(db, 2))["opened"] == 0
    assert query(database, "SELECT count(*) FROM outages") == [(0,)]
    assert tape(database, "pulse_down") == []
    ((severity, occurrences, acknowledged, data, message),) = query(database, ALERTS)
    assert (severity, occurrences, acknowledged) == ("major", 3, False)
    assert data == {
        "product_id": alpha,
        "network": "alpha/residential",
        "share": 1.0,
        "refused": 500,
        "payment": 0,
        "requests": 500,
        "regions": ["ams3", "fra1", "lon1", "nyc3", "tor1"],
        "since": window(0).isoformat(),
        "window_start": window(2).isoformat(),
    }
    assert message.startswith("alpha/residential: 100% of its pulse requests in 5 regions")
    assert "since 2026-10-05 12:00 UTC" in message


async def test_one_refused_region_changes_nothing(database, db, tmp_path):
    """407 in one region only: no alert, and the other regions' errors still open the outage."""
    prepare(database, REGIONS)
    (alpha,) = products(database, "alpha")
    await pulse_run(tmp_path, database, alpha, "DE", 0, refused=100)
    for country in ("GB", "NL", "US"):
        await pulse_run(tmp_path, database, alpha, country, 0, refused=0, other=80)
    assert (await evaluated(db, 0))["opened"] == 1
    assert len(tape(database, "pulse_down")) == 1
    assert query(database, ALERTS) == []


async def test_407_mixed_with_503_above_the_share_is_still_a_login_problem(database, db, tmp_path):
    prepare(database, REGIONS)
    (alpha,) = products(database, "alpha")
    for country in ("DE", "GB", "NL"):  # every request failed: 60 refused, 40 answered 503
        await pulse_run(tmp_path, database, alpha, country, 0, refused=60, other=40)
    assert (await evaluated(db, 0))["opened"] == 0
    assert query(database, "SELECT count(*) FROM outages") == [(0,)]
    ((_, _, _, data, _),) = query(database, ALERTS)
    assert (data["share"], data["regions"]) == (0.6, ["ams3", "fra1", "lon1"])


async def test_the_alert_clears_when_the_share_drops_and_a_later_hour_raises_a_new_one(
    database, db, tmp_path
):
    prepare(database, REGIONS)
    alpha, beta = products(database, "alpha", "beta")
    for country in ("DE", "GB"):
        await pulse_run(tmp_path, database, alpha, country, 0, refused=90)
        await pulse_run(tmp_path, database, beta, country, 0, refused=90, target="generic_https")
        await pulse_run(tmp_path, database, alpha, country, 1, refused=30)  # below the share
        await pulse_run(tmp_path, database, alpha, country, 12, refused=100)  # the next hour
    await evaluated(db, 0)
    assert [row[2] for row in query(database, ALERTS)] == [False, False]
    await evaluated(db, 1)  # alpha's share dropped; beta has no data: its alert stays open
    found = [(row[3]["product_id"], row[2]) for row in query(database, ALERTS)]
    assert found == [(alpha, True), (beta, False)]
    await evaluated(db, 12)
    rows = query(database, ALERTS)
    assert len(rows) == 3 and rows[2][2] is False
    assert (rows[2][3]["product_id"], rows[2][3]["since"]) == (alpha, window(12).isoformat())


async def test_outages_already_open_are_not_touched(database, db, tmp_path):
    """A network outage open before the refusal is neither closed nor joined by another."""
    prepare(database, REGIONS)
    (alpha,) = products(database, "alpha")
    for country in REGIONS:
        await pulse_run(tmp_path, database, alpha, country, 0, refused=0, other=100)
        for k in (1, 2, 3):
            await pulse_run(tmp_path, database, alpha, country, k, refused=100)
    assert (await evaluated(db, 0))["opened"] == 1
    assert await evaluated(db, 3) == {"windows": 3, "opened": 0, "closed": 0, "latency_rise": 0}
    assert query(database, "SELECT DISTINCT started_at,ended_at FROM outages") == [
        (window(0), None)
    ]
    assert tape(database, "pulse_recovered") == []
    assert len(query(database, ALERTS)) == 1


async def test_a_window_without_requests_is_neither_refused_nor_cleared():
    stats = {"requests": 0, "refused": 0, "login": []}
    assert not pulse_login.refused(stats)
    assert await pulse_login.judge(None, W0, 1, stats) is None  # no query at all
