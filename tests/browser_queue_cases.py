"""The owner's queue (BROWSER-QUEUE-1) as a settings file: 3 hosts of 6 networks, the 8 browser
targets once a day at their measured cost, 2 browsers per host; shared by the queue tests."""

import tomllib
from datetime import UTC, datetime
from pathlib import Path

from test_browser_settings import write

from proxy_index.browser.settings import load_settings

EXAMPLE = Path(__file__).resolve().parents[1] / "configs" / "browser-pulse.example.toml"
# Browser-seconds of one 100-try run on its engine (the owner's numbers).
COSTS = {
    "ai_chatgpt": 5800,
    "cloudflare_challenge": 4520,
    "linkedin_public": 2760,
    "instagram_web": 2520,
    "ai_overview": 1470,
    "amazon_pdp": 1450,
    "ai_gemini": 1360,
    "google_search": 1350,
}
ORDER = [
    "ai_chatgpt",
    "google_search",
    "cloudflare_challenge",
    "ai_gemini",
    "linkedin_public",
    "amazon_pdp",
    "instagram_web",
    "ai_overview",
]
DAY = datetime(2026, 11, 8, tzinfo=UTC)


def networks(host: int, count: int = 6) -> list[str]:
    return [f"example-{host}{i}/residential" for i in range(count)]


def queue_document(hosts=(6, 6, 6)) -> dict:
    """The example settings in queue mode with the owner's numbers, enabled."""
    doc = tomllib.loads(EXAMPLE.read_text())
    doc["enabled"] = True
    doc["schedule"]["plan"] = "queue"
    doc["browser"]["browsers_per_host"] = 2
    for entry in doc["target"]:
        entry.update(every_minutes=1440, browser_seconds=COSTS[entry["id"]])
    doc["host"] = [
        {"name": f"browser-{h + 1}", "networks": networks(h + 1, n)} for h, n in enumerate(hosts)
    ]
    del doc["networks"]
    return doc


def queue_settings(tmp_path, doc=None):
    return load_settings(write(tmp_path, doc or queue_document()))
