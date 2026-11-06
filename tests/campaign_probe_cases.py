"""Shared fakes for the `campaign probe` tests: run summaries, the probe remote and a world.

The probe runs against the in-process fake DigitalOcean and a fake remote that answers each
step with a run summary; no request or SSH connection leaves the machine.
"""

from __future__ import annotations

import json
import re
import tomllib
from datetime import datetime
from pathlib import Path

from campaign_fakes import FakeDigitalOcean, no_sleep, running
from campaign_hour_cases import make_root, make_spec

from proxy_index.campaign import probe as probing
from proxy_index.campaign.contracts import spec_path
from proxy_index.campaign.deploy import toml_table
from proxy_index.campaign.probe import ProbeInputs

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.fromisoformat("2027-01-01T22:00:00+00:00")
IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def summary(completed=25_000, successes=None, statuses=None, errors=None, rps=900.0):
    statuses, errors = statuses or {}, errors or {}
    failed = sum(statuses.get(s, 0) for s in ("429", "469", "502", "503", "504")) + sum(
        errors.values()
    )
    return {
        "completed": completed,
        "successes": completed - failed if successes is None else successes,
        "total_errors": failed,
        "status_codes": {"200": 1, **statuses},
        "errors": {f"http_{s}": n for s, n in statuses.items()} | errors,
        "requests_per_second": rps,
        "latency_ms": {"p50": 820, "p95": 2400},
        "stop_reason": None,
        "unique_ips": 24_000,
        "config": {"gateway": "203.0.113.9"},
    }


GOOD = summary()


def classified(**fields):
    """A run summary with RL-1's answer classes: 1,000 completed, 30 429s and 20 CONNECT
    refusals rate-limited, 5 no-IP 465s, 10 other-country exits among 800 successes."""
    return {
        "completed": 1000,
        "successes": 800,
        "total_errors": 200,
        "unique_ips": 700,
        "status_codes": {"200": 800, "429": 30, "465": 5, "503": 10},
        "errors": {"http_status": 45, "proxy_connect": 45, "timeout": 20, "tls": 90},
        "requests_per_second": 50.0,
        "latency_ms": {"p50": 700, "p95": 1900},
        "stop_reason": "requests",
        "rate_limited": {"429": 30, "proxy_connect": 20},
        "no_inventory": 5,
        "other_country": 10,
    } | fields


class Recording(FakeDigitalOcean):
    """Also keeps every create body."""

    def __init__(self):
        super().__init__()
        self.creates = []

    async def _create(self, request):
        self.creates.append(await request.json())
        return await super()._create(request)


class FakeProbeRemote:
    """Answers each (network, concurrency) step; records every command and the setup tar."""

    def __init__(self, answers, setup=b"installing\nready\n", ready=True):
        self.answers, self.setup, self.ready = answers, setup, ready
        self.calls, self.folders = [], []

    async def wait_ready(self, droplet, limit_seconds):
        return self.ready

    async def run(self, droplet, command, what, *, stdin=None, seconds=120):
        self.calls.append((command, stdin, what))
        if command[0] == "python3":
            if isinstance(self.setup, Exception):
                raise self.setup
            return self.setup
        key = (
            command[command.index("--provider") + 1],
            int(command[command.index("--concurrency") + 1]),
        )
        answer = self.answers.get(key, GOOD)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, bytes):
            return answer
        return ("Results: /opt/x\n" + json.dumps(answer) + "\n").encode()

    def factory(self, folder):
        self.folders.append(folder)
        return self


PROBED = ("soax", "webshare", "iproyal")


def world(tmp_path, networks=PROBED[:2]):
    """A manager with a planned campaign: real census configs and 0600 credentials."""
    spec = make_spec(networks, ("lon1", "nyc3"))
    root = make_root(tmp_path, spec)
    spec_path(root).write_text(spec.to_json())
    for network in networks:
        config = ROOT / "configs" / f"{network}.toml"
        (root / spec.manifests[network]).with_name("provider.toml").write_bytes(config.read_bytes())
        names = tomllib.loads(config.read_text())["credentials"]
        credentials = tmp_path / "manager" / ".secrets" / "credentials" / f"{network}.toml"
        credentials.write_bytes(
            toml_table(
                network,
                {
                    key.removesuffix("_env"): "acct9"
                    if key == "account_env"
                    else f"secret-{network}"
                    for key in names
                },
            )
        )
        credentials.chmod(0o600)
    return ProbeInputs(root, tmp_path / "manager" / "probes"), spec


async def probed(fake, remote, inputs):
    async with running(fake) as cloud:
        return await probing.probe(cloud, remote.factory, inputs, now=NOW, sleep=no_sleep)
