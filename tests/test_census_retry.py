"""HTTP 469 retry policy: real attempts remain auditable, census samples exclude them."""
import asyncio
import time

import pytest
from aiohttp import web
from helpers import FakeTransport

from proxy_index import JsonSink, ProxyConfig, RunConfig, run
from proxy_index.models import Outcome


def config(**overrides):
    return RunConfig("generic_http", ProxyConfig("http", "127.0.0.1", 8080),
                     **({"requests": None, "concurrency": 1, "stop_rule": "unique_ratio",
                         "min_sample": 10, "retry_http_469": True} | overrides))


async def test_469s_do_not_trigger_saturation_but_remain_in_attempts_and_bytes(tmp_path):
    class Responses(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            if 10 < self.count <= 510:
                return Outcome(1, error="http_status", status=469, bytes_up=5, bytes_down=7)
            return Outcome(1, ip=f"192.0.2.{(self.count - 1) % 6 + 1}", status=200)

    progress = []
    result = await run(config(), JsonSink(tmp_path), transport_factory=Responses,
                       progress=progress.append)
    assert result.stop_reason == "unique_ratio"
    assert result.completed == 513 and result.census_requests == 13
    assert result.http_469_responses == result.status_codes["469"] == 500
    assert result.successes == 13 and result.unique_ips == 6
    assert result.total_errors == 500
    assert result.bytes_up == 2500 and result.bytes_down == 3500
    assert progress[-1]["requests"] == 513 and progress[-1]["census_requests"] == 13
    assert progress[-1]["http_469_responses"] == 500
    assert result.config["census_rule_version"] == 3
    assert result.config["census_ratio_basis"] == "completed_requests_excluding_http_469"


async def test_469s_do_not_consume_minimum_sample_or_no_success_count(tmp_path):
    class Responses(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            return Outcome(1, error="http_status", status=469 if self.count <= 20 else 503)

    result = await run(config(min_sample=4), JsonSink(tmp_path), transport_factory=Responses)
    assert result.stop_reason == "no_success"
    assert result.completed == 24 and result.census_requests == 4
    assert result.status_codes == {"469": 20, "503": 4}


async def test_other_failures_still_count_toward_unique_ratio(tmp_path):
    class Responses(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            if self.count <= 20:
                return Outcome(1, error="http_status", status=469)
            if self.count == 21:
                return Outcome(1, ip="2001:db8::1", status=200)
            return Outcome(1, error="timeout")

    result = await run(config(min_sample=4), JsonSink(tmp_path), transport_factory=Responses)
    assert result.stop_reason == "unique_ratio"
    assert result.completed == 24 and result.census_requests == 4
    assert result.successes == 1 and result.timeouts == 3


async def test_all_469_retries_are_bounded_by_deadline(tmp_path):
    class RateLimited(FakeTransport):
        async def execute(self, target, *, context=None):
            await asyncio.sleep(.001)
            return Outcome(1, error="http_status", status=469)

    result = await run(config(min_sample=2), JsonSink(tmp_path), transport_factory=RateLimited,
                       deadline=time.time() + .1)
    assert result.completed > 2 and result.census_requests == 0
    assert result.http_469_responses == result.completed
    assert result.stop_reason == "deadline"


async def test_explicit_attempt_cap_and_legacy_policy_remain_bounded(tmp_path):
    class RateLimited(FakeTransport):
        async def execute(self, target, *, context=None):
            return Outcome(1, error="http_status", status=469)

    result = await run(config(requests=25, min_sample=2), JsonSink(tmp_path / "retry"),
                       transport_factory=RateLimited)
    assert result.completed == 25 and result.census_requests == 0
    assert result.stop_reason == "requests"
    legacy = await run(config(retry_http_469=False, min_sample=2),
                       JsonSink(tmp_path / "legacy"), transport_factory=RateLimited)
    assert legacy.completed == 2 and legacy.stop_reason == "no_success"
    assert legacy.config["census_rule_version"] == 2
    assert "retry_http_469" not in legacy.config
    assert "census_requests" not in legacy.to_dict()


def test_policy_requires_census_and_strict_boolean():
    with pytest.raises(ValueError, match="census"):
        config(stop_rule="count", requests=10)
    with pytest.raises(ValueError, match="boolean"):
        config(retry_http_469=1)


async def test_four_real_workers_retry_469_with_one_shared_census_denominator(tmp_path):
    counts = {"469": 0, "200": 0}

    async def echo(request):
        status = 469 if sum(counts.values()) % 2 == 0 else 200
        counts[str(status)] += 1
        await asyncio.sleep(.001)
        return web.Response(status=status, text="limited" if status == 469 else "2001:db8::1")

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", echo)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await run(RunConfig(
            "generic_http", ProxyConfig("http", "127.0.0.1", port),
            target_url="http://example.test/", requests=None, concurrency=12, processes=4,
            stop_rule="unique_ratio", min_sample=20, retry_http_469=True,
        ), JsonSink(tmp_path))
    finally:
        await server.cleanup()
    assert result.stop_reason == "unique_ratio"
    assert result.config["execution_processes"] == 4
    assert result.status_codes == counts
    assert 20 <= result.census_requests <= 31
    assert result.census_requests == result.successes == counts["200"]
    assert result.completed == result.census_requests + result.http_469_responses
