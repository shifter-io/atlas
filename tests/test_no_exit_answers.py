"""NO-EXIT-404-1: on the echo targets a gateway's "no exit in this country" 404 is not measured,
for every network; any other 404 (inside the tunnel, or with a body) and a 404 on a browser
target stay failures (atlas/no_exit_answers.py, outcome.echo_404)."""

import json
from datetime import timedelta

import pytest
from calculated_pulse_cases import T0, after, pulses
from census_pulse_cases import product
from helpers import Gateway, server
from orchestrator_cases import prepare, query
from pulse_cases import evaluated, products, run, tape, window
from test_pulse_country_verdicts import verdict
from test_pulse_no_exit import JUDGED, OUTAGES, REGIONS, judged, stored

from atlas import JsonSink, ProxyConfig, RunConfig, RunOptions, no_exit_answers
from atlas import run as measure
from atlas.browser.targets.pages import AmazonPdp
from atlas.calculated import incremental, policy, rollups, store
from atlas.daily import tape as daily_tape
from atlas.daily.expected import statuses
from atlas.models import Outcome
from atlas.no_exit_answers import (
    ECHO,
    SCOPES,
    count,
    exclude,
    live,
    rule,
    slots,
    split,
    strip,
    targets,
)
from atlas.outcome import NO_EXIT, TARGET_404, Verdict, echo_404
from atlas.regions import CONFIGS
from atlas.storage import connect, seed
from atlas.targets import EchoIpTarget, RequestSpec, TargetRegistry
from atlas.transport import _echo_404

pytest_plugins = ["test_storage", "orchestrator_cases"]

RULE = {"no_exit_answers": {"targets": ["generic_http", "generic_https"]}}
GATEWAY_404 = {"404": 100, "proxy_connect": 100}  # stored before the rule: a CONNECT refused
NEW_404 = {"404": 100, "no_exit": 100}  # recorded since the rule


# The classification of what the client sees.


def test_the_gateway_answers_the_connect_or_plain_http_with_an_empty_body():
    assert echo_404("connect", "proxy_connect", 0) == (NO_EXIT, Verdict.PROXY_ERROR)
    assert echo_404("plain", "http_status", 0) == (NO_EXIT, Verdict.PROXY_ERROR)
    # An exit device's own page (103 bytes) or an ISP's block page: the target side.
    assert echo_404("plain", "http_status", 103) == (TARGET_404, None)
    assert echo_404("plain", "body_too_large", 513) == (TARGET_404, None)
    assert echo_404("tunnel", "http_status", 0) == (TARGET_404, None)


def test_only_an_echo_targets_404_is_reclassified():
    proxy = ProxyConfig("http", "127.0.0.1", 9)
    plain, tls = RequestSpec("http://ip-info.com/myip"), RequestSpec("https://ip-info.com/myip")
    echo = EchoIpTarget("generic_http", plain.url)
    answer = Outcome(1.0, error="http_status", status=404, verdict=Verdict.ERROR)
    assert _echo_404(answer, echo, plain, proxy).error == NO_EXIT
    inside = Outcome(1.0, error="http_status", status=404, response_body_bytes=0)
    assert _echo_404(inside, echo, tls, proxy).error == TARGET_404  # inside the tunnel
    for other in (Outcome(1.0, error="http_status", status=503), Outcome(1.0, status=200)):
        assert _echo_404(other, echo, plain, proxy) is other
    assert _echo_404(answer, AmazonPdp(), plain, proxy) is answer  # a browser target: unchanged


PAGE = b"<H1>404 Not Found</H1>\n"  # an exit device's own page


@pytest.mark.parametrize(
    ("gateway", "url", "error", "proxy_errors"),
    [
        (lambda: Gateway(status=404, body=b""), "http://localhost:9/", NO_EXIT, 3),
        (lambda: Gateway(status=404, body=PAGE), "http://localhost:9/", TARGET_404, 0),
        (lambda: Gateway(auth=lambda *_: False, refusal=404), "https://localhost:9/", NO_EXIT, 3),
    ],
)
async def test_a_run_records_the_gateways_404_apart(tmp_path, gateway, url, error, proxy_errors):
    """Through a loopback gateway: a plain-HTTP 404 without a body and a 404 to the CONNECT are
    the gateway's; a 404 page is the target side's. Both stay in the run's raw counts."""
    gateway = gateway()
    targets = TargetRegistry()
    targets.register(EchoIpTarget("echo", url))
    async with server(gateway) as port:
        proxy = ProxyConfig("http", "127.0.0.1", port, "u", "p")
        config = RunConfig("echo", proxy, requests=3, concurrency=1)
        summary = await measure(config, JsonSink(tmp_path), RunOptions(targets=targets))
    assert (summary.completed, summary.errors, summary.status_codes) == (3, {error: 3}, {"404": 3})
    assert summary.proxy_errors == proxy_errors


# The rule on stored counts.


def _row(statuses, errors, body=0, successes=0):
    found = {"errors": errors, "response_body_bytes": body, "successes": successes}
    return found if statuses is None else found | {"status_codes": statuses}


# (row, left out with scope "gateway", left out with scope "every")
COUNTS = [
    (_row({"404": 7}, {"no_exit": 7}), 7, 7),  # recorded since the rule: exact
    (_row({"404": 7}, {"no_exit": 4, "target_404": 3}, 900), 4, 7),
    (_row({"200": 9, "404": 3}, {"target_404": 3}, 1030, 9), 0, 3),  # pages on the path
    # Stored before the rule: no body, so the gateway's (10 IPs of 14.5 bytes each)
    (_row({"200": 10, "404": 5}, {"http_status": 5}, 145, 10), 5, 5),
    (_row({"200": 10, "404": 2}, {"http_status": 2}, 745, 10), 0, 2),  # two 300-byte pages
    (_row({"404": 5}, {"proxy_connect": 2, "timeout": 3}), 2, 2),  # a CONNECT answered 404
    # Ten 404 block pages and ten real connection failures: the pages take http_status first.
    (_row({"404": 10}, {"http_status": 10, "proxy_connect": 10}, 3000), 0, 10),
    (_row({"404": 5}, {"http_status": 5, "proxy_connect": 10}), 5, 5),  # empty plain 404s
    (_row({"404": 5}, {"body_too_large": 5}, 2565), 0, 5),  # a large block page
    (_row({"200": 5}, {}, 72, 5), 0, 0),
    (_row(None, {"no_exit": 3, "target_404": 2, "http_status": 2}), 3, 5),  # an old copy
]


@pytest.mark.parametrize(("counters", "gateway", "every"), COUNTS)
def test_count_leaves_out_the_404s_of_the_scope(counters, gateway, every):
    assert count(counters) == count(counters, "gateway") == gateway
    assert count(counters, "every") == every


@pytest.mark.parametrize("scope", SCOPES)
def test_the_live_sql_counts_as_the_calculation(database, monkeypatch, scope):
    """Detection's SQL (run summaries, run_buckets rows) and count() agree, in both scopes."""
    monkeypatch.setattr(no_exit_answers, "live", lambda: (ECHO, scope))
    rows = [c for c, _, _ in COUNTS if "status_codes" in c]
    left = no_exit_answers.sql("r.summary", "r.target")
    proxy = no_exit_answers.sql("r.summary", "r.target", proxy=True)
    summary = f"SELECT {left},{proxy} FROM (SELECT %s::jsonb AS summary,%s AS target) r"
    with connect(database) as conn:
        for row in rows:
            parts = split(row, scope)
            wanted = (sum(parts.values()), sum(parts.get(c, 0) for c in no_exit_answers.PROXY))
            for target, expected in (("generic_https", wanted), ("amazon_pdp", (0, 0))):
                found = conn.execute(summary, (json.dumps(row), target)).fetchone()
                assert tuple(found) == expected, (row, target)


def test_strip_takes_the_tries_and_their_error_codes():
    counters = {"completed": 10, "successes": 4, "proxy_errors": 3}
    counters["errors"] = {"proxy_connect": 3, "http_status": 2, "timeout": 1}
    found = strip(counters, {"proxy_connect": 3, "http_status": 1})
    assert found["completed"] == 6 and found["successes"] == 4
    assert found["errors"] == {"http_status": 1, "timeout": 1} and found["proxy_errors"] == 0
    assert counters["completed"] == 10  # the stored row is not changed


def test_empty_plain_404s_leave_the_real_proxy_errors():
    """Five empty plain 404s and ten connection failures stored before the rule: the 404s
    leave http_status, the ten proxy errors stay."""
    counters = _row({"404": 5}, {"http_status": 5, "proxy_connect": 10})
    counters |= {"completed": 15, "proxy_errors": 10}
    assert split(counters) == {"http_status": 5}
    found = strip(counters, split(counters))
    assert found["completed"] == 10 and found["proxy_errors"] == 10
    assert found["errors"] == {"proxy_connect": 10}


def _bucket(key, completed, successes, statuses, errors):
    counters = {"completed": completed, "successes": successes, "errors": errors}
    return {"run_key": key, "counters": counters | {"status_codes": statuses}}


def test_the_calculation_keeps_every_other_count():
    """A Generic HTTPS run answered 404 by its gateway loses those tries; its rate is the rest's.
    The browser run's 404 and every bucket without a gateway 404 are the same objects."""
    runs = [
        {"run_key": 1, "scope": {"target": {"id": "generic_https"}}},
        {"run_key": 2, "scope": {"target": {"id": "amazon_pdp"}}},
        {"run_key": 3, "scope": {"target": {"id": "generic_http"}}},
    ]
    buckets = [
        _bucket(1, 100, 40, {"200": 40, "404": 60}, {"proxy_connect": 60}),
        _bucket(1, 50, 0, {"404": 50}, {"proxy_connect": 50}),
        _bucket(2, 10, 5, {"404": 5}, {"http_status": 5}),
        _bucket(3, 100, 98, {"200": 98, "502": 2}, {"proxy_connect": 2}),
    ]
    kept, gone = exclude(RULE, runs, buckets)
    assert gone == {1: 110}
    assert [b["counters"]["completed"] for b in kept] == [40, 10, 100]  # the all-404 one dropped
    assert kept[1] is buckets[2] and kept[2] is buckets[3]
    assert kept[0]["counters"]["errors"] == {}
    pulsed = [{"run_key": 1, "requests": 150}, {"run_key": 3, "requests": 100}]
    assert slots(pulsed, gone) == [{"run_key": 1, "requests": 40}, pulsed[1]]
    assert slots([{"run_key": 1, "requests": 110}], gone) == []  # nothing measured: no slot
    assert exclude({}, runs, buckets) == (buckets, {})  # without the setting: as before


@pytest.mark.parametrize(("scope", "completed"), [("gateway", [100]), ("every", [98])])
def test_a_page_on_the_path_is_left_out_only_with_scope_every(scope, completed):
    """Two 300-byte 404 pages among 98 successes: failures with "gateway", left out with
    "every"."""
    runs = [{"run_key": 1, "scope": {"target": {"id": "generic_http"}}}]
    bucket = _bucket(1, 100, 98, {"200": 98, "404": 2}, {"http_status": 2})
    bucket["counters"]["response_body_bytes"] = 98 * 14 + 600
    config = {"no_exit_answers": RULE["no_exit_answers"] | {"scope": scope}}
    kept, _ = exclude(config, runs, [bucket])
    assert [b["counters"]["completed"] for b in kept] == completed


def test_the_calculation_adjusts_buckets_and_pulse_slots_together():
    runs = [{"run_key": 1, "scope": {"target": {"id": "generic_http"}}}]
    buckets = [_bucket(1, 100, 0, {"404": 100}, {"http_status": 100})]
    scheduled = {"pulses": [{"run_key": 1, "requests": 100, "successes": 0}]}
    assert incremental._measured(RULE, runs, buckets, scheduled) == []
    assert scheduled["pulses"] == []


def test_the_methodology_names_the_echo_targets_and_the_scope():
    config, _ = policy.load(CONFIGS / "methodology.toml")
    assert rule(config) == live() == (ECHO, "gateway")
    for wrong in (["amazon_pdp"], "generic_http", ["generic_http"] * 2):
        with pytest.raises(ValueError, match="no_exit_answers"):
            targets({"no_exit_answers": {"targets": wrong}})
    for extra in ({"status": 404}, {"scope": "all"}):
        with pytest.raises(ValueError, match="no_exit_answers"):
            rule({"no_exit_answers": {"targets": []} | extra})
    assert rule({}) == (frozenset(), "gateway") and targets({}) == frozenset()
    every = {"no_exit_answers": {"targets": ["generic_http"], "scope": "every"}}
    assert rule(every) == ({"generic_http"}, "every")


# Detection, outages and the country status.


@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize("answers", [GATEWAY_404, NEW_404])
async def test_a_country_answering_only_the_gateways_404_opens_no_outage(
    database, tmp_path, fast, answers
):
    """Regression: a network whose gateway answers 404 to every Generic HTTPS pulse of pulses 6
    and 7 got a 'down' and an outage; now nothing is judged, and pulse 8 is judged as before."""
    seed(database)
    keys = await stored(tmp_path, database, [answers] * 2)
    with connect(database) as conn:
        alpha = product(conn)
        run_keys = {6: keys[0], 7: keys[1]}
        pulses(conn, alpha, [95] * 6 + [0, 0, 95], run_keys=run_keys, target="generic_https")
    with connect(database) as conn:
        daily_tape.replay(conn, T0, after(9), fast=fast)
        assert conn.execute(JUDGED).fetchall() == []
        assert conn.execute(OUTAGES).fetchall() == []


@pytest.mark.parametrize("fast", [False, True])
async def test_a_pulse_is_judged_on_its_other_answers(database, tmp_path, fast):
    """60 of 100 answers are the gateway's 404 and 38 of the other 40 succeed: 95%, as the
    baseline (38 of 100 was a success_drop)."""
    seed(database)
    (key,) = await stored(tmp_path, database, [{"404": 60, "no_exit": 60}])
    with connect(database) as conn:
        alpha = product(conn)
        pulses(conn, alpha, [95] * 6 + [38], run_keys={6: key})
    with connect(database) as conn:
        daily_tape.replay(conn, T0, after(7), fast=fast)
        assert conn.execute(JUDGED).fetchall() == []


async def test_a_404_page_of_the_target_side_stays_a_failure(database, tmp_path):
    """Two such pulses: the first waits, the second is 'down' (PULSE-NODE-DNS-2)."""
    seed(database)
    keys = await stored(tmp_path, database, [{"404": 100, "target_404": 100}] * 2)
    with connect(database) as conn:
        alpha = product(conn)
        pulses(conn, alpha, [95] * 6 + [0, 0], run_keys=dict(zip((6, 7), keys, strict=True)))
    with connect(database) as conn:
        events = [(alpha, "pulse_finished", None), (alpha, "down", None)]
        assert judged(conn, 8) == (events, [(alpha, "DE", True)])


async def test_the_cleanup_closes_an_outage_a_gateway_404_opened(database, db, tmp_path):
    """Outages opened before the rule: the one whose opening pulse was all the gateway's 404 ends
    at its start ('not_measured', no downtime); the one opened by timeouts stays open."""
    prepare(database, REGIONS)
    keys = await stored(tmp_path, database, [GATEWAY_404, {"timeout": 100}])
    (alpha,) = products(database, "alpha")
    with connect(database) as conn:
        for country, key in zip(("DE", "GB"), keys, strict=True):
            verdict(conn, alpha, country, T0 - timedelta(days=1), 80)  # both countries count
            pulses(conn, alpha, [0], run_keys={0: key}, country=country, target="generic_https")
        conn.execute(
            "INSERT INTO outages(product_id,country_code,source,batch_id,started_at) "
            "SELECT %s,b.country_code,'pulse',b.batch_id,b.scheduled_for FROM test_batches b",
            (alpha,),
        )
    assert (await evaluated(db, 0))["closed"] == 1
    found = query(
        database,
        "SELECT country_code,ended_at=started_at,end_reason FROM outages ORDER BY country_code",
    )
    assert found == [("DE", True, "not_measured"), ("GB", None, None)]


async def test_the_evaluator_leaves_the_gateways_404_out_of_its_window(database, db, tmp_path):
    """The gateway answers 404 to every Generic HTTP pulse in DE and GB, NL times out: with the
    404s the window would be down (280 of 500, 3 regions); without them it is 80 of 300."""
    prepare(database, REGIONS)
    (alpha,) = products(database, "alpha")
    for country, gone, timeouts in (("DE", 100, 0), ("GB", 100, 0), ("NL", 0, 60)):
        rows = [(window(0), 100, gone + timeouts, 300)]
        key = await run(tmp_path, database, "pulse", window(0), alpha, country, rows)
        with connect(database) as conn:
            conn.execute(
                "UPDATE run_buckets SET status_codes=%s,errors=%s,proxy_errors=%s,timeouts=%s "
                "WHERE run_key=%s",
                (json.dumps({"404": gone}), json.dumps({NO_EXIT: gone}), gone, timeouts, key),
            )
    for country in ("CA", "US"):
        rows = [(window(0), 100, 10, 300)]
        await run(tmp_path, database, "pulse", window(0), alpha, country, rows)
    assert (await evaluated(db, 0))["opened"] == 0
    assert tape(database, "pulse_down") == []


def test_a_country_answered_only_with_the_gateways_404_has_no_exits():
    row = {"network": "alpha", "country": "BV", "unique_ips": 0, "requests": 100, "completed": 100}
    row |= {"statuses": {"404": 100}, "errors": {"http_status": 100}, "previous": 0, "recent": 0}
    row |= {"decided_at": T0 + timedelta(hours=1)}
    assert statuses([row], {}) == {"alpha": {"BV": "no_exits"}}
    row |= {"statuses": {"503": 100}, "errors": {"proxy_connect": 100}}
    assert statuses([row], {}) == {"alpha": {"BV": "failing"}}  # alpha has no "no exit" code


async def test_a_recount_copies_again_an_hour_copied_before_the_rule(database, tmp_path):
    """A pulse bucket answered 404 by the gateway, stored before the rule. An hour copied before
    it has no status codes, so the hourly build keeps the 404s; a recount copies it again."""
    seed(database)
    store.initialize(database)  # the calculated layer
    (alpha,) = products(database, "alpha")
    rows = [(window(0), 100, 100, 300)]
    key = await run(tmp_path, database, "pulse", window(0), alpha, "DE", rows, "generic_https")
    start, as_of = window(0) - timedelta(days=30), window(0) + timedelta(hours=2)
    with connect(database) as conn:
        conn.execute(
            "UPDATE test_runs r SET batch_id=br.batch_id FROM batch_runs br "
            "WHERE br.run_key=r.run_key"
        )
        conn.execute(
            "UPDATE run_buckets SET status_codes=%s,errors=%s WHERE run_key=%s",
            (json.dumps({"404": 100}), json.dumps({"proxy_connect": 100}), key),
        )
        (copied,) = rollups.scheduled_rows(conn, start, as_of)
        assert count(copied["counters"]) == 100  # the copy keeps the status codes of a 404
        conn.execute("UPDATE calculated_pulse_buckets SET extra=extra-'status_codes'")
        (old,) = rollups.scheduled_rows(conn, start, as_of)
        assert "status_codes" not in old["counters"] and count(old["counters"]) == 0
        (again,) = rollups.scheduled_rows(conn, start, as_of, recopy=True)
        assert again["counters"] == copied["counters"] and count(again["counters"]) == 100


@pytest.mark.parametrize(("gone", "closed"), [(95, 0), (75, 1)])
async def test_the_stale_cleanup_counts_a_pulse_by_its_other_answers(
    database, db, tmp_path, gone, closed
):
    """An open DE outage and a later pulse with 5 successes of 100 answers, `gone` of them the
    gateway's 404: with 5 other answers (under min_requests) it does not end the outage, with
    25 it does."""
    prepare(database, REGIONS)
    (key,) = await stored(tmp_path, database, [{"404": gone, NO_EXIT: gone}])
    (alpha,) = products(database, "alpha")
    with connect(database) as conn:
        verdict(conn, alpha, "DE", T0 - timedelta(days=1), 80)
        pulses(conn, alpha, [0, 5], run_keys={1: key})
        conn.execute(
            "INSERT INTO outages(product_id,country_code,source,batch_id,started_at) "
            "SELECT %s,b.country_code,'pulse',b.batch_id,b.scheduled_for FROM test_batches b "
            "ORDER BY b.scheduled_for LIMIT 1",
            (alpha,),
        )
    assert (await evaluated(db, 0))["closed"] == closed
    found = query(database, "SELECT ended_at IS NULL FROM outages")
    assert found == [(not closed,)]
