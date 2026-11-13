"""Reproducible simulated scale test. Does not contact public proxy/target hosts."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import ipaddress
import json
import platform
import resource
import time
from pathlib import Path

from atlas import JsonSink, ProxyConfig, RunConfig, RunOptions, run
from atlas.models import Outcome


class SyntheticTransport:
    def __init__(self, config):
        self.count = 0
        self.active = 0
        self.peak = 0
        self.config = config

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, target, *, context=None):
        self.count += 1
        index = self.count
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        return Outcome(
            (index % 100) + 0.1,
            ip=str(ipaddress.IPv4Address(0x0B000000 + index)),
            status=200,
            response_body_bytes=16,
        )


async def benchmark(requests, concurrency, output):
    config = RunConfig(
        "generic_http",
        ProxyConfig("http", "unused.invalid", 8080),
        requests=requests,
        concurrency=concurrency,
    )
    transport = SyntheticTransport(config)
    sink = JsonSink(output)
    start = time.perf_counter()
    summary = await run(config, sink, RunOptions(transport_factory=lambda _: transport))
    elapsed = time.perf_counter() - start
    with gzip.open(sink.directory / "ips.jsonl.gz", "rt") as stream:
        rows = 0
        count = 0
        for line in stream:
            row = json.loads(line)
            count += row["count"]
            rows += 1
    assert summary.completed == summary.successes == summary.unique_ips == requests
    assert count == rows == requests
    assert transport.peak <= concurrency
    assert summary.latency_ms == {"p50": 50, "p90": 90, "p95": 95, "p99": 99}
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report = {
        "mode": "simulated; no network I/O",
        "requests": requests,
        "unique_ips": rows,
        "concurrency": concurrency,
        "peak_in_flight": transport.peak,
        "wall_seconds_including_export": elapsed,
        "peak_rss_mib": rss / (1024**2 if platform.system() == "Darwin" else 1024),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "run_directory": str(sink.directory),
        "passed": True,
    }
    (Path(output) / "benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=5_000)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--output", default="artifacts/scale")
    args = parser.parse_args()
    if args.requests % 100:
        parser.error("Synthetic percentile fixture requires a multiple of 100 requests")
    asyncio.run(benchmark(args.requests, args.concurrency, args.output))
