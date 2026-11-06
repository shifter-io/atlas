"""Shared helpers of the local onboarding simulations: the campaign start, a `manifest` fixture,
a fake clock, proxy and measurement. test_onboarding.py keeps these names importable for the
other test modules that use them.

The test modules load it with `pytest_plugins` after importing its names, so pytest cannot
rewrite it; it holds no asserts (PYTEST_DONT_REWRITE).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from proxy_index import ProxyConfig
from proxy_index.onboarding import manifest as campaign

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2027, 1, 1, 22, tzinfo=UTC)


@pytest.fixture
def manifest(tmp_path):
    return campaign.create(
        ROOT / "configs/shifter.toml",
        "residential",
        campaign.timestamp(START),
        tmp_path / "onboarding-networks",
        now=START - timedelta(hours=1),
    )


class Clock:
    def __init__(self, now):
        self.value = now

    def now(self):
        return self.value

    async def sleep(self, seconds):
        # Scheduler idle waits advance to the next UTC hour; the deadline watcher
        # observes the same clock without accelerating an active country's work.
        if seconds >= 0.2:
            self.value = (self.value // 3600 + 1) * 3600
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(0.001)


class Proxy:
    def __init__(self, country):
        self.country = country.lower()

    def public(self):
        return {"provider": "shifter", "product": "residential", "requested_country": self.country}

    def resolve(self, context):
        return ProxyConfig("http", "localhost", 8080)


def fake_measurement(**extra):
    values = dict(status="completed", stop_reason="unique_ratio", completed=1000, total_errors=0)
    values.update(extra)
    return SimpleNamespace(**values, to_dict=lambda: values)
