"""DAILY-DIAG-1: a run's tries, successes and error classes per 5 seconds (atlas.slices)."""

import json

from helpers import FakeTransport
from runner_cases import config

from atlas import JsonSink, RunOptions, run
from atlas.models import Outcome
from atlas.slices import LIMIT, Slices, error_class

OK = Outcome(1, ip="8.8.8.8", status=200)


def failed(error, status=None):
    return Outcome(1, error=error, status=status, verdict="error")


def test_error_classes_split_target_statuses_and_keep_transport_codes():
    assert error_class(OK) is None
    assert error_class(Outcome(1, error="timeout")) == "timeout"
    assert error_class(Outcome(1, error="proxy_connect", status=502)) == "proxy_connect"
    assert error_class(Outcome(1, error="dns")) == "dns"
    assert error_class(failed("http_status", 429)) == "http_429"
    assert error_class(failed("http_status", 404)) == "http_4xx"
    assert error_class(failed("http_status", 503)) == "http_5xx"
    assert error_class(failed("http_status", 301)) == "http_status"


def test_each_try_counts_in_the_5_second_slice_it_ended_in():
    slices = Slices()
    slices.add(0.2, OK)
    slices.add(4.99, Outcome(1, error="timeout"))
    slices.add(12.0, OK)
    slices.add(13.0, failed("http_status", 429))
    assert slices.export() == {
        "seconds": 5,
        "tries": [2, 0, 2],
        "successes": [1, 0, 1],
        "errors": {"http_429": [0, 0, 1], "timeout": [1, 0, 0]},
    }
    assert Slices().export() == {"seconds": 5, "tries": [], "successes": [], "errors": {}}


def test_a_long_run_widens_its_slices_and_keeps_every_count():
    slices = Slices()
    for second in range(0, 3600, 2):
        slices.add(second, OK if second % 4 else Outcome(1, error="timeout"))
    kept = slices.export()
    assert kept["seconds"] == 40 and len(kept["tries"]) == 90 <= LIMIT
    assert sum(kept["tries"]) == 1800 and sum(kept["successes"]) == 900
    assert kept["errors"]["timeout"][:2] == [10, 10] and sum(kept["errors"]["timeout"]) == 900
    edge = Slices()
    edge.add(LIMIT * 5 - 0.1, OK)
    assert edge.seconds == 5 and len(edge.export()["tries"]) == LIMIT
    edge.add(LIMIT * 5, OK)
    assert edge.seconds == 10 and edge.export()["tries"][-2:] == [1, 1]


def test_a_full_busy_run_stays_well_under_10_kb():
    slices = Slices()
    tries = [OK, Outcome(1, error="timeout"), Outcome(1, error="proxy_connect")]
    tries += [Outcome(1, error="dns"), failed("http_status", 429), failed("http_status", 503)]
    for index in range(LIMIT):
        for outcome in tries:
            slices.add(index * 5 + 1, outcome)
    for counts in (slices.tries, slices.successes, *slices.errors.values()):
        for index in counts:
            counts[index] *= 2000  # five-digit counts in every slice and class
    assert len(json.dumps(slices.export())) < 10_000


async def test_a_run_with_slices_keeps_them_in_its_summary(tmp_path):
    class Mixed(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            return [
                Outcome(1, ip="8.8.8.8", status=200),
                Outcome(1, error="timeout"),
                Outcome(1, error="http_status", status=403, verdict="block"),
                Outcome(1, error="proxy_connect"),
            ][(self.count - 1) % 4]

    sink = JsonSink(tmp_path)
    options = RunOptions(transport_factory=Mixed, slices=True)
    result = await run(config(requests=8, concurrency=1), sink, options)
    saved = json.loads((sink.directory / "summary.json").read_text())["slices"]
    assert saved == result.slices and saved["seconds"] == 5
    assert sum(saved["tries"]) == result.completed == 8 and sum(saved["successes"]) == 2
    errors = {name: sum(counts) for name, counts in saved["errors"].items()}
    assert errors == {"http_4xx": 2, "proxy_connect": 2, "timeout": 2}
    assert {len(column) for column in [saved["tries"], *saved["errors"].values()]} == {
        len(saved["successes"])
    }
    plain = await run(
        config(requests=2), JsonSink(tmp_path), RunOptions(transport_factory=FakeTransport)
    )
    assert plain.slices is None and "slices" not in plain.to_dict()
