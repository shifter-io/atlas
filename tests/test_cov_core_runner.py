"""runner.py, sinks.py, stats.py and node_tls.py paths their own tests do not reach (COV-7):
the run refusals, a geo artifact of more than one 1,000-row chunk, a sink asked to serve two
runs, an error without a typed counter and an unreadable CA file."""

import gzip
import json
from itertools import count

import pytest
from helpers import FakeTransport
from runner_cases import config

from proxy_index import JsonSink, RunConfig, RunOptions, run
from proxy_index.errors import UserError
from proxy_index.models import Outcome
from proxy_index.node_tls import pinned_context
from proxy_index.stats import Counters
from proxy_index.targets import RequestSpec, TargetRegistry


class Fixed:
    """A target that is not an echo-IP target, on any engine."""

    version, pool_eligible = "1", False

    def __init__(self, target_id, engine="http"):
        self.id, self.engine = target_id, engine

    def build_request(self):
        return RequestSpec("http://example.test/")

    def assess(self, status, body):
        raise AssertionError("never requested")


def only(target):
    targets = TargetRegistry()
    targets.register(target)
    return targets


async def test_a_url_override_needs_an_echo_ip_target(tmp_path):
    options = RunOptions(targets=only(Fixed("generic_http")))
    with pytest.raises(ValueError, match="URL overrides require an echo-IP target"):
        await run(config(target_url="http://example.test/ip"), JsonSink(tmp_path), options)
    assert list(tmp_path.iterdir()) == []  # refused before the sink started


async def test_a_browser_target_needs_its_own_adapter(tmp_path):
    options = RunOptions(targets=only(Fixed("shop", engine="browser")))
    with pytest.raises(ValueError, match="different execution adapter"):
        await run(RunConfig("shop", config().proxy), JsonSink(tmp_path), options)


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), "2026-01-01", True])
async def test_a_deadline_must_be_a_finite_timestamp(tmp_path, deadline):
    options = RunOptions(transport_factory=FakeTransport, deadline=deadline)
    with pytest.raises(ValueError, match="deadline must be a finite Unix timestamp"):
        await run(config(), JsonSink(tmp_path), options)


class Distinct(FakeTransport):
    """Every answer from a new documentation address."""

    numbers = count(1)

    async def execute(self, target, *, context=None):
        return Outcome(5.0, ip=f"2001:db8::{next(self.numbers):x}", status=200)


class CityPerAddress:
    def lookup(self, address):
        return {"country_code": "DE", "city": f"city-{address}"}


async def test_more_than_1000_geo_rows_in_a_bucket_are_written_in_chunks(tmp_path):
    sink = JsonSink(tmp_path)
    options = RunOptions(
        transport_factory=Distinct,
        enricher=CityPerAddress(),
        wall_clock=lambda: 1000.0,  # one 5-minute bucket for the whole run
    )
    chunks = []
    geo = sink.geo
    sink.geo = lambda rows: (chunks.append(len(rows)), geo(rows))
    result = await run(config(requests=1001, concurrency=8), sink, options)
    assert result.unique_ips == 1001
    assert chunks == [1000, 1]
    with gzip.open(sink.directory / "geo.jsonl.gz", "rt") as stream:
        rows = [json.loads(line) for line in stream]
    assert len(rows) == 1001 and {row["completed"] for row in rows} == {1}
    assert len({row["location"]["city"] for row in rows}) == 1001


def test_a_sink_refuses_a_second_run_while_one_is_open(tmp_path):
    sink = JsonSink(tmp_path)
    sink.start("first")
    with pytest.raises(RuntimeError, match="cannot serve concurrent runs"):
        sink.start("second")
    sink.abort()
    assert [path.name for path in tmp_path.iterdir()] == ["first"]
    sink.start("second")  # a closed stream frees the sink
    sink.abort()


def test_an_error_without_a_typed_counter_counts_only_as_an_error():
    counters = Counters()
    counters.add(Outcome(12.0, error="protocol", bytes_up=40, bytes_down=10))
    assert counters.completed == 1 and counters.successes == 0
    assert counters.errors == {"protocol": 1}
    assert counters.verdicts() == {
        "challenges": 0,
        "blocks": 0,
        "timeouts": 0,
        "proxy_errors": 0,
        "bytes_up": 40,
        "bytes_down": 10,
    }
    assert counters.histogram.counts.total() == 0


def test_an_unreadable_ca_file_is_refused_without_its_path(tmp_path):
    missing = tmp_path / "orchestrator.crt"
    with pytest.raises(UserError, match="^the orchestrator CA file cannot be read$"):
        pinned_context(missing)
