"""Answer classes (census rule 4): rate limits are replaced, no-IP and other-country answers
count like duplicates, and a run with only rate limits stops after two minutes (the CLI
flags: test_answer_classes_cli.py)."""

import asyncio
import gzip
import json

import pytest
from aiohttp import web
from answer_classes_cases import (
    GEONODE,
    PLAIN,
    Countries,
    census,
    config,
    dropping_gateway,
    limited,
    ok,
    refused,
    scripted,
    timeout,
)
from helpers import server

from proxy_index import JsonSink, ProxyConfig, RunConfig, RunOptions, run
from proxy_index.answers import Answers
from proxy_index.models import AnswerClasses, Outcome
from proxy_index.providers.grammar import answer_classes


async def test_rl1_429s_do_not_consume_the_request_goal(tmp_path):
    progress = []
    result = await run(
        config(requests=100, concurrency=10),
        JsonSink(tmp_path),
        RunOptions(
            progress=progress.append,
            transport_factory=scripted(lambda n: limited() if n % 2 else ok(f"192.0.2.{n % 7}")),
        ),
    )
    assert result.completed == 200 and result.successes == 100
    assert result.rate_limited == {"429": 100} and result.total_errors == 100
    assert result.status == "completed" and result.stop_reason == "requests"
    assert result.started == 200 and result.not_started == 0 and result.cancelled == 0
    assert result.no_inventory == 0 and result.other_country is None
    assert result.config["answer_classes"] == {"rate_limits": ["429"], "no_inventory": []}
    assert progress[-1]["rate_limited"] == 100 and "census_requests" not in progress[-1]
    saved = json.loads((tmp_path / result.run_id / "summary.json").read_text())
    assert saved["rate_limited"] == {"429": 100} and "other_country" not in saved


async def test_rl1_connect_refusals_are_rate_limits_only_while_succeeding(tmp_path):
    answers = [
        refused(0),
        ok("192.0.2.1", 1),
        refused(50),
        ok("192.0.2.2", 100),
        refused(219),
        ok("192.0.2.3", 220),
        refused(341),
    ]
    result = await run(
        config(requests=5, answer_classes=answer_classes("dataimpulse")),
        JsonSink(tmp_path),
        RunOptions(transport_factory=scripted(answers)),
    )
    assert result.completed == 7 and result.successes == 3
    assert result.rate_limited == {"429": 0, "proxy_connect": 2}
    # Before the first success and 121 s after the latest one, a refusal is a real error.
    assert result.errors == {"proxy_connect": 4} and result.stop_reason == "requests"


async def test_rl1_rate_limit_safety_stop_after_two_minutes_without_success(tmp_path):
    result = await run(
        config(requests=1000),
        JsonSink(tmp_path / "only"),
        RunOptions(transport_factory=scripted(lambda n: limited(at=10.0 * (n - 1)))),
    )
    assert result.stop_reason == "rate_limited" and result.status == "completed"
    assert result.completed == 13 and result.successes == 0  # stops at t = 120 s

    def mixed(n):  # a success, then rate limits mixed with timeouts
        if n == 1:
            return ok("192.0.2.1", 0)
        return limited(at=10.0 * n) if n % 2 else timeout(10.0 * n)

    result = await run(
        config(requests=1000),
        JsonSink(tmp_path / "mixed"),
        RunOptions(transport_factory=scripted(mixed)),
    )
    assert result.stop_reason == "rate_limited" and result.completed == 12  # t = 120 s
    assert result.rate_limited == {"429": 5} and result.timeouts == 6


async def test_rl1_no_safety_stop_without_rate_limits(tmp_path):
    result = await run(
        config(requests=40),
        JsonSink(tmp_path),
        RunOptions(transport_factory=scripted(lambda n: timeout(10.0 * n))),
    )
    assert result.stop_reason == "requests" and result.completed == 40

    def recovering(n):  # rate limits for 100 s, then a success: the window restarts
        return ok("192.0.2.1", 10.0 * n) if n in (1, 12, 23) else limited(at=10.0 * n)

    result = await run(
        config(requests=3),
        JsonSink(tmp_path / "recovering"),
        RunOptions(transport_factory=scripted(recovering)),
    )
    assert result.stop_reason == "requests" and result.completed == 23


async def test_rl1_census_rule_4_excludes_rate_limits_from_the_sample(tmp_path):
    def answers(n):
        return limited() if 10 < n <= 510 else ok(f"192.0.2.{(n - 1) % 6 + 1}")

    progress = []
    result = await run(
        census(),
        JsonSink(tmp_path),
        RunOptions(transport_factory=scripted(answers), progress=progress.append),
    )
    assert result.stop_reason == "unique_ratio"
    assert result.completed == 513 and result.census_requests == 13
    assert result.rate_limited == {"429": 500} and result.unique_ips == 6
    assert result.bytes_up == 2500 and result.http_469_responses is None
    assert result.config["census_rule_version"] == 4
    assert result.config["census_ratio_basis"] == "completed_requests_excluding_rate_limits"
    assert progress[-1]["census_requests"] == 13 and progress[-1]["rate_limited"] == 500


async def test_rl1_469_and_429_together_for_geonode(tmp_path):
    def answers(n):
        return limited(469 if n % 2 else 429) if n <= 20 else limited(503)

    result = await run(
        census(min_sample=4, answer_classes=answer_classes("geonode")),
        JsonSink(tmp_path),
        RunOptions(transport_factory=scripted(answers)),
    )
    assert result.stop_reason == "no_success"
    assert result.completed == 24 and result.census_requests == 4
    assert result.rate_limited == {"429": 10, "469": 10} and result.no_inventory == 0
    assert result.status_codes == {"469": 10, "429": 10, "503": 4}


async def test_ctry_other_country_exits_count_like_duplicates(tmp_path):
    def answers(n):  # odd: a new German exit each time; even: one of three US exits
        return ok(f"198.51.100.{n}") if n % 2 else ok(f"192.0.2.{(n // 2 - 1) % 3 + 1}")

    seen = []

    async def observe(ip, at):
        seen.append(ip)

    result = await run(
        census(requested_country="us"),
        JsonSink(tmp_path),
        RunOptions(
            enricher=Countries(), transport_factory=scripted(answers), observation_consumer=observe
        ),
    )
    assert result.stop_reason == "unique_ratio" and result.completed == 10
    assert result.successes == 10 and result.other_country == 5 and result.unique_ips == 3
    assert result.census_requests == 10 and set(seen) == {"192.0.2.1", "192.0.2.2", "192.0.2.3"}
    with gzip.open(tmp_path / result.run_id / "ips.jsonl.gz", "rt") as stream:
        rows = [json.loads(line) for line in stream]
    assert {row["ip"] for row in rows} == set(seen) and sum(r["count"] for r in rows) == 5


async def test_ctry_all_other_country_stops_at_min_sample(tmp_path):
    result = await run(
        census(requested_country="US"),
        JsonSink(tmp_path),
        RunOptions(
            enricher=Countries(), transport_factory=scripted(lambda n: ok(f"198.51.100.{n}"))
        ),
    )
    assert result.stop_reason == "unique_ratio" and result.completed == 10
    assert result.unique_ips == 0 and result.other_country == 10


async def test_ctry_unmatched_address_counts_in_country(tmp_path):
    result = await run(
        config(requests=20, requested_country="US"),
        JsonSink(tmp_path),
        RunOptions(
            enricher=Countries(), transport_factory=scripted(lambda n: ok(f"203.0.113.{n}"))
        ),
    )
    assert result.unique_ips == 20 and result.other_country == 0
    # Without an enricher the check is off: other_country is not reported.
    plain = await run(
        config(requests=2, requested_country="US"),
        JsonSink(tmp_path / "off"),
        RunOptions(transport_factory=scripted(lambda n: ok(f"198.51.100.{n}"))),
    )
    assert plain.unique_ips == 2 and plain.other_country is None


async def test_ctry_no_inventory_status_counts_in_sample_not_as_unique(tmp_path):
    result = await run(
        census(answer_classes=GEONODE),
        JsonSink(tmp_path / "empty"),
        RunOptions(transport_factory=scripted(lambda n: limited(465))),
    )
    assert result.stop_reason == "no_success" and result.completed == 10
    assert result.no_inventory == 10 and result.total_errors == 10
    assert result.census_requests == 10

    def thin(n):  # three exits, otherwise "no exits in this country"
        return ok(f"192.0.2.{n}") if n <= 3 else limited(465)

    result = await run(
        census(answer_classes=GEONODE),
        JsonSink(tmp_path / "thin"),
        RunOptions(transport_factory=scripted(thin)),
    )
    assert result.stop_reason == "unique_ratio" and result.completed == 10
    assert result.no_inventory == 7 and result.unique_ips == 3


def test_rl1_rule_3_and_rule_4_are_exclusive():
    with pytest.raises(ValueError, match="retry_http_469"):
        census(retry_http_469=True)
    with pytest.raises(ValueError, match="AnswerClasses"):
        config(answer_classes={"rate_limits": ["429"]})
    for rate_limits, no_inventory in (
        (["429"], ()),
        (("429", "429"), ()),
        (("200",), ()),
        (("429",), ("proxy_connect",)),
        ((429,), ()),
        (("429",), ("429",)),
        (("469",), ("469",)),
    ):
        with pytest.raises(ValueError, match="Answer class|disjoint"):
            AnswerClasses(rate_limits, no_inventory)


async def test_rl1_configs_without_answer_classes_are_unchanged(tmp_path):
    count = config(answer_classes=None)
    assert "answer_classes" not in count.public()
    assert census(answer_classes=None).public()["census_rule_version"] == 2
    progress = []
    first_limited = scripted(lambda n: limited() if n == 1 else ok("192.0.2.8"))
    result = await run(
        count,
        JsonSink(tmp_path),
        RunOptions(progress=progress.append, transport_factory=first_limited),
    )
    # Without answer classes a 429 is an ordinary answer: it consumes the request goal.
    assert result.completed == 100 and result.successes == 99
    saved = result.to_dict()
    for key in ("rate_limited", "no_inventory", "other_country", "census_requests"):
        assert key not in saved and key not in progress[-1]
    # Without answer classes the class counter stays empty, even with an enricher.
    plain = Answers(census(answer_classes=None, requested_country="US"), Countries())
    assert plain.add(ok("198.51.100.1"), 0.0) == "answer" and plain.halt(500.0) is None


async def test_rl1_two_processes_share_one_answered_goal(tmp_path):
    counts = {"429": 0, "200": 0}

    async def echo(request):
        status = 429 if sum(counts.values()) % 3 == 2 else 200
        counts[str(status)] += 1
        await asyncio.sleep(0.001)
        return web.Response(status=status, text="limited" if status == 429 else "2001:db8::1")

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", echo)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await run(
            RunConfig(
                "generic_http",
                ProxyConfig("http", "127.0.0.1", port),
                target_url="http://example.test/",
                requests=30,
                concurrency=6,
                processes=2,
                answer_classes=PLAIN,
            ),
            JsonSink(tmp_path),
        )
    finally:
        await server.cleanup()
    assert result.config["execution_processes"] == 2
    assert result.stop_reason == "requests" and result.status_codes == counts
    assert result.successes == 30 and result.rate_limited == {"429": counts["429"]}
    assert result.completed == 30 + counts["429"] and counts["429"] > 0


@pytest.mark.parametrize("processes", [1, 2])
async def test_rl1b_dropped_gateway_connections_are_rate_limits_while_succeeding(
    tmp_path, processes
):
    served = {"connections": 0}
    async with server(dropping_gateway(served, after=6)) as port:
        result = await run(
            RunConfig(
                "generic_http",
                ProxyConfig("http", "127.0.0.1", port),
                target_url="http://example.test/",
                requests=30,
                concurrency=6,
                processes=processes,
                answer_classes=answer_classes("dataimpulse"),
            ),
            JsonSink(tmp_path),
        )
    dropped = result.errors.get("connection", 0)
    assert dropped > 0 and result.errors == {"connection": dropped}
    # Every drop came while successes continued: each is a rate limit, replaced by an answer.
    assert result.rate_limited == {"429": 0, "proxy_connect": dropped}
    assert result.successes == 30 and result.completed == 30 + dropped == served["connections"]
    assert result.stop_reason == "requests"


def test_rl1b_only_refused_or_dropped_connections_join_the_connect_signal():
    def dropped(at):
        return Outcome(1, error="connection", completed_at=at)

    answers = Answers(config(answer_classes=answer_classes("dataimpulse")))
    assert answers.add(dropped(0), 0) == "answer"  # before any success: a real error
    answers.add(ok("192.0.2.1"), 1)
    assert [
        answers.add(outcome, at)
        for outcome, at in (
            (dropped(50), 50),
            (refused(60), 60),
            (Outcome(1, error="proxy_auth"), 70),
            (Outcome(1, error="dns"), 80),
            (dropped(121), 121),
        )
    ] == ["rate_limited", "rate_limited", "answer", "answer", "answer"]
    assert answers.rate_limited == {"429": 0, "proxy_connect": 2}
    # A network without rate_limit_connect keeps every dropped connection a real error.
    plain = Answers(config(answer_classes=answer_classes("soax")))
    plain.add(ok("192.0.2.1"), 0)
    assert plain.add(dropped(1), 1) == "answer" and plain.excluded == 0
