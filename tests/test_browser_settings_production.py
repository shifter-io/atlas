"""BROWSER-HOSTS-SETTINGS-1: the production settings template loads with the real loader and
gives each mini its 22-hour queue from queue_start (test_browser_queue_cycle.py) and, before it,
its daily queue: 54 runs 26 or 27 minutes apart, each on a whole minute (a bundle's slot),
offsets of a third of the gap; every target on the owner's engine: Instagram and X both on
Chrome, so social_web is one engine's score (SOCIAL-ENGINE-1)."""

import fnmatch
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from atlas.browser.chrome import DEFAULT_EXECUTABLE
from atlas.browser.contracts import SOCIAL_SOURCES, slot_stamp
from atlas.browser.settings import load_settings
from atlas.orchestrator.browser_queue import EPOCH, queues

CONFIGS = Path(__file__).resolve().parents[1] / "configs"
TEMPLATE = CONFIGS / "browser-pulse.production.example.toml"
ENGINES = {
    "amazon_pdp": "chrome",
    "linkedin_public": "chrome",
    "instagram_web": "chrome",
    "x_web": "chrome",
    "ai_gemini": "chrome",
    "google_search": "camoufox",
    "cloudflare_challenge": "camoufox",
    "ai_chatgpt": "camoufox",
    "ai_overview": "camoufox",
}
COSTS = {
    "amazon_pdp": 1450,
    "linkedin_public": 2760,
    "instagram_web": 2520,
    "x_web": 740,
    "ai_gemini": 1360,
    "google_search": 1350,
    "cloudflare_challenge": 4520,
    "ai_chatgpt": 5800,
    "ai_overview": 1470,
}
HOSTS = {
    "browser-1": ["decodo", "geonode", "iproyal", "oxylabs", "shifter", "soax"],
    "browser-2": ["dataimpulse", "evomi", "infatica", "massive", "nodemaven", "webshare"],
    "browser-3": [
        "byteful",
        "fleetproxy",
        "proxy-cheap",
        "proxy-seller",
        "proxyempire",
        "rayobyte",
    ],
}
PERIOD = timedelta(days=1)


def test_the_template_loads_with_the_production_fields():
    settings = load_settings(TEMPLATE)
    schedule, browser = settings.schedule, settings.browser
    assert settings.enabled is False
    assert (schedule.plan, schedule.queue_period_minutes, schedule.run_deadline_minutes) == (
        "queue",
        1320,
        180,
    )
    assert schedule.queue_start == datetime(2026, 11, 18, tzinfo=UTC)
    assert schedule.countries == ("US",) and schedule.lead_seconds == 60
    assert (browser.browsers_per_host, browser.recycle_after, browser.engine) == (2, 1, "camoufox")
    assert browser.block_resources == ("image", "media", "font")
    assert browser.cache_hours == 24
    # BROWSER-BYTES-1 and -2: these read once their HTML is parsed; the others wait for load.
    waits = {t.id: t.wait_until for t in settings.targets if t.wait_until is not None}
    early = ("google_search", "ai_overview", "amazon_pdp", "linkedin_public", "instagram_web")
    assert waits == dict.fromkeys((*early, "x_web"), "domcontentloaded")
    assert browser.wait_until == "load"
    blocks = {t.id: t.block_urls for t in settings.targets if t.block_urls is not None}
    blocked = {"ai_overview", "ai_chatgpt", "ai_gemini", "cloudflare_challenge"}
    assert set(blocks) == blocked and browser.block_urls == ()
    # Never what the overview needs: Google's scripts, its app and its answers.
    needed = ("https://www.google.com/xjs/_/js/k=x", "https://www.gstatic.com/_/mss/x")
    assert not any(fnmatch.fnmatchcase(u, p) for u in needed for p in blocks["ai_overview"])
    # Its scripts and app are kept warm instead (public files only, asset_cache.py).
    overview = next(t for t in settings.targets if t.id == "ai_overview")
    assert all(any(fnmatch.fnmatchcase(u, p) for p in overview.cache_urls) for u in needed)
    cached = {t.id for t in settings.targets if t.cache_urls}
    assert cached == {"ai_overview", "ai_chatgpt", "instagram_web", "x_web", "linkedin_public"}
    assert settings.chrome.executable == DEFAULT_EXECUTABLE
    assert {t.id: (t.tries, t.every_minutes) for t in settings.targets} == {
        target: (100, 1320) for target in ENGINES
    }
    assert {t.id: t.browser_seconds for t in settings.targets} == COSTS
    assert {t.id: settings.engine(t.id) for t in settings.targets} == ENGINES
    assert settings.engines() == ("camoufox", "chrome")
    assert {h.name: list(h.networks) for h in settings.hosts} == {
        name: [f"{provider}/residential" for provider in providers]
        for name, providers in HOSTS.items()
    }
    assert settings.untargeted == ("proxy-cheap/residential",)


def test_chatgpt_caches_its_app_files_and_blocks_the_sign_in_prompt():
    # BROWSER-BYTES-3: about 3.1 MB a try before, about 0.36 MB once the cache holds the files.
    [chatgpt] = [t for t in load_settings(TEMPLATE).targets if t.id == "ai_chatgpt"]
    assert chatgpt.block_resources == ("image", "media", "font")
    assert chatgpt.block_urls == ("https://accounts.google.com/gsi/*",)
    assert "https://chatgpt.com/unauth-mweb/assets/*" in chatgpt.cache_urls
    # The page, its anti-bot script and the answer are never cached.
    assert not any("sentinel" in url or "backend" in url for url in chatgpt.cache_urls)


def test_gemini_blocks_only_what_its_answer_never_needs():
    # BROWSER-BYTES-3: about 5.6 MB a try before, about 4.7 MB after, success unchanged.
    [gemini] = [t for t in load_settings(TEMPLATE).targets if t.id == "ai_gemini"]
    assert "https://www.googletagmanager.com/*" in gemini.block_urls
    assert gemini.cache_urls is None
    # The page, its scripts, its anti-bot checks and the answer stream are never blocked.
    needed = (
        "https://gemini.google.com/app",
        "https://gemini.gstatic.com/_/mss/boq-bard-web/_/js/k=x",
        "https://www.google.com/js/bg/x.js",
        "https://gemini.google.com/_/BardChatUi/data/x/StreamGenerate",
    )
    assert not any(fnmatch.fnmatchcase(u, rule) for u in needed for rule in gemini.block_urls)


def test_linkedin_caches_only_its_content_hashed_stylesheets():
    # BROWSER-BYTES-5: about 0.12 MB a try with an empty cache, about 0.054 MB warm.
    [linkedin] = [t for t in load_settings(TEMPLATE).targets if t.id == "linkedin_public"]
    assert linkedin.cache_urls == ("https://static.licdn.com/aero-v1/sc/h/*",)
    # Only the content-hashed static files: never a profile, the authwall or an API.
    pages = ("https://www.linkedin.com/in/x/", "https://www.linkedin.com/authwall")
    assert not any(fnmatch.fnmatchcase(u, p) for u in pages for p in linkedin.cache_urls)


def test_the_challenge_target_never_blocks_or_caches_the_challenge():
    # BROWSER-BYTES-3: about 1.76 MB a try before, about 1.16 MB with these blocks and the
    # private file's cache rules, results unchanged.
    [cf] = [t for t in load_settings(TEMPLATE).targets if t.id == "cloudflare_challenge"]
    challenge = (
        "https://challenges.cloudflare.com/turnstile/v0/api.js",
        "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/orchestrate/x",
        "https://www.upwork.com/cdn-cgi/challenge-platform/scripts/jsd/main.js",
        "https://www.glassdoor.com/cdn-cgi/challenge-platform/h/g/scripts/x.js",
        "https://www.scrapingcourse.com/cdn-cgi/challenge-platform/h/b/orchestrate/x.js",
    )
    assert cf.cache_urls is None  # the input sites' paths live only in the private file
    assert not any(fnmatch.fnmatchcase(u, rule) for u in challenge for rule in cf.block_urls)


def test_only_linkedin_starts_chrome_without_its_own_google_calls():
    # BROWSER-BYTES-5: about 54 KB a warm try with them, about 41 KB without; same verdicts.
    settings = load_settings(TEMPLATE)
    quiet = {t.id for t in settings.targets if t.chrome_services is False}
    assert quiet == {"linkedin_public"} and settings.browser.chrome_services is True


def test_instagram_and_x_run_on_one_engine_so_social_web_is_one_score():
    # The calculator never adds two engines (BROWSER-ENGINE-FIGURES-1): only sources on one
    # engine make social_web one combined score.
    settings = load_settings(TEMPLATE)
    assert {source: settings.engine(source) for source in SOCIAL_SOURCES} == {
        "instagram_web": "chrome",
        "x_web": "chrome",
    }


def test_before_queue_start_each_mini_runs_its_daily_54_runs_26_or_27_minutes_apart():
    settings = load_settings(TEMPLATE)
    hosts = queues(settings, daily=True)
    gap = timedelta(minutes=80) / 3
    assert [q.offset for q in hosts] == [gap * h / 3 for h in range(3)]
    day = (datetime(2026, 11, 9, tzinfo=UTC) - EPOCH) // PERIOD
    for queue in hosts:
        assert (queue.period, queue.until) == (PERIOD, settings.schedule.queue_start)
        entries = queue.entries(day, timedelta(seconds=60))
        starts = [entry.start for entry in entries]
        assert len(set(starts)) == 54
        assert {b - a for a, b in pairwise(starts)} == {timedelta(minutes=m) for m in (26, 27)}
        # A queue run's bundle slot is its start: each must be a whole minute (X-TARGET-1).
        assert all(slot_stamp(start) for start in starts)
        assert sorted((e.network, e.target) for e in entries) == sorted(
            (network, target) for network in queue.networks for target in ENGINES
        )
        # Nothing of the daily queue from queue_start on.
        assert queue.entries(queue.day(queue.until), timedelta(seconds=60)) == []


def test_page_targets_block_only_what_their_rules_never_read():
    """BROWSER-BYTES-2: Amazon and LinkedIn judge their server HTML, so their scripts are
    blocked (stylesheets stay: Amazon's "Buy used" price is visible text); Instagram and X need
    their scripts to show the posts, and READY waits for them, so only stylesheets go."""
    own = {t.id: t.block_resources for t in load_settings(TEMPLATE).targets}
    base = ("image", "media", "font")
    assert own["amazon_pdp"] == own["linkedin_public"] == (*base, "script")
    assert own["instagram_web"] == own["x_web"] == (*base, "stylesheet")
