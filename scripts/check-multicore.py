#!/usr/bin/env python3
"""Local CPU-only process-backend check. No sockets to proxies or external traffic."""
import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from milf_framework.models import Outcome, ProxyConfig, RequestContext, RunConfig
from milf_framework.onboarding.files import atomic_json
from milf_framework.process_transport import ProcessTransport
from milf_framework.targets import registry


class CPUWork:
    def __init__(self, config):
        self.seconds = config.timeout_seconds

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, target, *, context):
        began = time.process_time()
        while time.process_time() - began < self.seconds:
            sum(i * i for i in range(1000))
        return Outcome(latency_ms=(time.process_time() - began) * 1000, ip="192.0.2.1")


async def check(processes, jobs, seconds):
    config = RunConfig("generic_http", ProxyConfig("http", "localhost", 1),
                       concurrency=jobs, processes=processes, timeout_seconds=seconds)
    target = registry.get("generic_http")
    async with ProcessTransport(config, target, factory=CPUWork) as workers:
        began = time.perf_counter()
        results = await asyncio.gather(*(
            workers.execute(target, context=RequestContext("local-cpu-check", i, i))
            for i in range(jobs)
        ))
        elapsed = time.perf_counter() - began
        cpu = sum(result.latency_ms for result in results) / 1000
        return {"workers": processes, "worker_pids": workers.pids, "wall_seconds": elapsed,
                "cpu_seconds": cpu, "occupied_cores": cpu / elapsed}


async def main(args):
    serial = await check(1, args.processes, args.seconds)
    parallel = await check(args.processes, args.processes, args.seconds)
    result = {"cpu_count": os.cpu_count(), "network_requests": 0,
              "serial": serial, "parallel": parallel,
              "speedup": serial["wall_seconds"] / parallel["wall_seconds"]}
    atomic_json(args.output, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processes", type=int, default=4)
    parser.add_argument("--seconds", type=float, default=2)
    parser.add_argument("--output", type=Path, default=Path(".state/multicore-check.json"))
    asyncio.run(main(parser.parse_args()))
