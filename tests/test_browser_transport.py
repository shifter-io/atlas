"""BrowserTransport over a fake driver and the real pool: outcomes, timings, recorder, budget."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from typing import Any

import browser_transport_cases
import pytest
from browser_site import SiteTarget
from browser_transport_cases import HTTP_PROXY, OPTIONS, SETTINGS, Harness, Script

from proxy_index.browser import chrome
from proxy_index.browser.contracts import FIREFOX_ERROR_PREFIXES, FIREFOX_ERRORS, TIMINGS
from proxy_index.browser.driver import NavigationError, UserAgentRefused
from proxy_index.browser.judge import OK, failed
from proxy_index.browser.targets.pages import GoogleSearch
from proxy_index.browser.transport import CONSENT_ACCEPT, BrowserTransport, NullRecorder
from proxy_index.models import ProxyConfig, RequestContext
from proxy_index.runner import RunOptions, run
from proxy_index.sinks import JsonSink
from proxy_index.targets import TargetRegistry


async def test_a_successful_try_is_ok_with_timings_bytes_and_one_record():
    harness = Harness()
    outcome = await harness.execute()
    assert outcome.verdict == "ok" and outcome.error is None and outcome.status == 200
    assert outcome.bytes_down == 4321 and outcome.bytes_exact is False
    [(record, shot)] = harness.recorder.tries
    assert shot is None and record["screenshot"] is None
    assert record["verdict"] == "ok" and record["input"] == "ok" and record["source"] is None
    assert set(record["timings_ms"]) == set(TIMINGS)
    assert all(v is not None for v in record["timings_ms"].values())
    assert record["final_url"] == "http://site.invalid/ok" and record["bytes_down_est"] == 4321
    browser = harness.browsers[0]
    assert browser.contexts == [(HTTP_PROXY, ("image", "media"))]
    assert browser.gotos == [("http://site.invalid/ok", 20_000)] and browser.open_contexts == 0
    assert browser.timeouts == [20_000]


@pytest.mark.parametrize("error", [None, RuntimeError("crashed"), NavigationError("ERR_FAILED")])
async def test_a_chrome_try_records_its_fingerprint_however_it_ends(monkeypatch, error):
    fingerprint = chrome.fingerprint("run:0", "US").record()
    monkeypatch.setattr(browser_transport_cases.FakePage, "fingerprint", fingerprint, raising=False)
    harness = Harness(Script(error=error))
    outcome = await harness.execute()
    assert outcome.error == (None if error is None else "browser_error")
    [(record, _)] = harness.recorder.tries
    assert record["fingerprint"] == fingerprint


async def test_a_status_999_try_is_recorded_not_raised():
    """LinkedIn answers its block with 999: the try keeps its record and the run goes on."""
    harness = Harness(Script(status=999))
    outcome = await harness.execute()
    assert outcome.status == 999
    [(record, _)] = harness.recorder.tries
    assert record["status"] == 999 and record["verdict"] == outcome.verdict


NAMES = [*FIREFOX_ERRORS, *(prefix + "X" for prefix, _ in FIREFOX_ERROR_PREFIXES), "OTHER", None]


@pytest.mark.parametrize("name", NAMES)
async def test_every_navigation_error_name_maps_by_k_bp2(name):
    harness = Harness(Script(error=NavigationError(name), url="about:blank"))
    outcome = await harness.execute()
    expected = dict(FIREFOX_ERRORS).get(name) or next(
        (code for prefix, code in FIREFOX_ERROR_PREFIXES if (name or "").startswith(prefix)),
        "browser_error",
    )
    assert outcome.error == expected
    [(record, shot)] = harness.recorder.tries
    assert record["error"] == expected and record["final_url"] is None and shot == b"jpeg"
    assert record["reason"] == ((name or "").lower() or "navigation_error")
    assert record["screenshot"] == "browser/screenshots/0000.jpg"


@pytest.mark.parametrize(
    ("policy", "fails", "taken"),
    [
        ("failures", True, True),
        ("failures", False, False),
        ("all", False, True),
        ("all", True, True),
        ("none", True, False),
    ],
)
async def test_screenshot_policy(policy, fails, taken):
    script = Script(found=0 if fails else 1)
    harness = Harness(script, options=replace(OPTIONS, screenshots=policy))
    outcome = await harness.execute(index=7)
    assert (outcome.error == "no_content") is fails
    [(record, shot)] = harness.recorder.tries
    assert (shot == b"jpeg") is taken
    assert record["screenshot"] == ("browser/screenshots/0007.jpg" if taken else None)


async def test_a_failed_screenshot_is_skipped():
    harness = Harness(Script(found=0, shot_error=True))
    await harness.execute()
    [(record, shot)] = harness.recorder.tries
    assert shot is None and record["screenshot"] is None


async def test_queue_wait_is_in_timings_not_in_latency():
    hold = asyncio.Event()
    harness = Harness(Script(hold=hold))
    first = asyncio.create_task(harness.execute(index=0))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(harness.execute(index=1))
    await asyncio.sleep(0.25)
    hold.set()
    await first
    outcome = await second
    record = harness.recorder.tries[1][0]
    assert record["timings_ms"]["queue"] >= 200
    assert outcome.latency_ms < 100
    assert outcome.latency_ms == record["timings_ms"]["navigate"] + record["timings_ms"]["settle"]


async def test_socks5_with_credentials_is_refused_before_any_traffic(caplog):
    socks = ProxyConfig("socks5", "192.0.2.10", 1080, "user", "pass")
    harness = Harness(proxy=socks)
    with caplog.at_level(logging.WARNING):
        for index in range(2):
            outcome = await harness.execute(index=index)
            assert (outcome.verdict, outcome.error) == ("proxy_error", "proxy_connect")
    assert harness.browsers == [] and harness.pool.stats()["leases"] == 0
    assert [t[0]["reason"] for t in harness.recorder.tries] == ["socks5_auth_unsupported"] * 2
    assert len([r for r in caplog.records if "SOCKS5" in r.getMessage()]) == 1
    assert "user" not in caplog.text and "pass" not in caplog.text


async def test_socks5_without_credentials_is_used():
    socks = ProxyConfig("socks5", "192.0.2.10", 1080)
    harness = Harness(proxy=socks)
    assert (await harness.execute()).verdict == "ok"


async def test_the_deadline_cancels_an_in_flight_try():
    settings = replace(SETTINGS, timeout_s=1)
    harness = Harness(Script(delay=30), settings=settings, deadline=time.time() - 0.9)
    began = time.perf_counter()
    outcome = await harness.execute()
    assert time.perf_counter() - began < 5
    assert (outcome.verdict, outcome.error) == ("timeout", "timeout")
    assert harness.recorder.tries[0][0]["reason"] == "deadline"
    assert harness.browsers[0].open_contexts == 0 and harness.pool._idle


async def test_a_crash_is_browser_error_and_the_pool_relaunches():
    harness = Harness(Script(error=RuntimeError("Target closed"), crash=True))
    outcome = await harness.execute()
    assert (outcome.verdict, outcome.error) == ("error", "browser_error")
    assert harness.recorder.tries[0][0]["reason"] == "runtimeerror"
    harness.script.crash, harness.script.error = False, None
    assert (await harness.execute(index=1)).verdict == "ok"
    assert len(harness.browsers) == 2 and harness.browsers[0].closed
    assert harness.pool.stats()["crashes"] == 1


async def test_a_failed_try_logs_its_type_but_not_its_message(caplog):
    harness = Harness(Script(error=RuntimeError("http://user:pass@192.0.2.10:8080")))
    outcome = await harness.execute()
    assert (outcome.verdict, outcome.error) == ("error", "browser_error")
    assert "Browser try failed (RuntimeError)" in caplog.text
    assert "pass@" not in caplog.text


class Raising(SiteTarget):
    async def interact(self, driver, spec) -> None:
        raise TimeoutError("an interact wait")


async def test_the_context_is_closed_when_the_target_raises():
    harness = Harness()
    outcome = await harness.execute(Raising())
    assert outcome.error == "browser_error"
    assert harness.recorder.tries[0][0]["reason"] == "timeouterror"
    assert harness.browsers[0].open_contexts == 0 and harness.browsers[0].alive()


async def test_a_refused_user_agent_stops_the_run():
    harness = Harness()

    async def refuse():
        raise UserAgentRefused("no")

    harness.pool._launch = refuse
    with pytest.raises(UserAgentRefused):
        await harness.execute()


GOOGLE = replace(SETTINGS, id="google_search", inputs=("pizza",))


async def test_a_google_consent_wall_is_accepted_once():
    found = {GoogleSearch.RESULTS: 3, CONSENT_ACCEPT: 1}
    script = Script(url="https://consent.google.com/ml", found=found)
    harness = Harness(script, settings=GOOGLE)
    outcome = await harness.execute(GoogleSearch())
    assert outcome.verdict == "ok" and harness.browsers[0].clicks == [CONSENT_ACCEPT]
    assert harness.recorder.tries[0][0]["final_url"] == "https://www.google.com/search"


@pytest.mark.parametrize(("results", "clicks"), [(3, 0), (0, 1)])
async def test_a_consent_form_is_accepted_only_when_it_hides_the_results(results, clicks):
    found = {GoogleSearch.RESULTS: results, GoogleSearch.CONSENT_FORM: 1, CONSENT_ACCEPT: 1}
    harness = Harness(Script(url="https://www.google.com/search", found=found), settings=GOOGLE)
    await harness.execute(GoogleSearch())
    assert len(harness.browsers[0].clicks) == clicks


async def test_target_overrides_win_over_browser_defaults():
    settings = replace(SETTINGS, timeout_s=3, block_resources=())
    harness = Harness(settings=settings)
    await harness.execute()
    assert harness.browsers[0].contexts == [(HTTP_PROXY, ())]
    assert harness.browsers[0].gotos[0][1] == 3000 and harness.browsers[0].timeouts == [3000]


async def test_a_consent_page_without_its_accept_control_is_judged_not_clicked():
    harness = Harness(Script(url="https://consent.google.com/ml", found={}), settings=GOOGLE)
    outcome = await harness.execute(GoogleSearch())
    assert (outcome.verdict, outcome.error) == ("error", "no_content")
    assert harness.recorder.tries[0][0]["reason"] == "consent_page"
    assert harness.browsers[0].clicks == []


async def test_refused_networks_are_logged_once_per_pool(caplog):
    socks = ProxyConfig("socks5", "192.0.2.10", 1080, "user", "pass")
    harness = Harness(proxy=socks)
    other = BrowserTransport(harness.config, pool=harness.pool, browser=OPTIONS, settings=SETTINGS)
    with caplog.at_level(logging.WARNING):
        await harness.execute()
        await other.execute(SiteTarget(), context=RequestContext("r", 0, 0))
    assert len([r for r in caplog.records if "SOCKS5" in r.getMessage()]) == 1


async def test_runs_through_the_runner_like_an_http_run(tmp_path):
    harness = Harness(Script(found=0), size=2)
    registry = TargetRegistry()
    registry.register(SiteTarget())
    factory = BrowserTransport.factory(
        harness.pool, harness.recorder, browser=OPTIONS, settings=SETTINGS
    )
    config = replace(harness.config, requests=6, concurrency=3)
    summary = await run(
        config, JsonSink(tmp_path), RunOptions(targets=registry, transport_factory=factory)
    )
    assert summary.completed == 6 and summary.successes == 0
    assert summary.errors == {"no_content": 6}
    assert sorted(t[0]["i"] for t in harness.recorder.tries) == list(range(6))


async def test_without_a_context_the_transport_numbers_its_tries():
    harness = Harness()
    async with harness.transport as transport:
        for _ in range(2):
            await transport.execute(SiteTarget())
    assert [t[0]["i"] for t in harness.recorder.tries] == [0, 1]
    NullRecorder().try_done({}, None)


@dataclass(frozen=True, slots=True)
class AnswerTarget(SiteTarget):
    """An AI-source-like target: interact() returns numbers that its rules read."""

    id: str = "ai_overview"
    numbers: Any = None

    async def interact(self, driver, spec):
        return self.numbers

    def rules(self, view):
        if view.found.get("answer_chars", 0) >= 80:
            return OK
        return failed("no_answer", "short_answer")


async def test_interact_numbers_reach_the_judge():
    harness = Harness()
    outcome = await harness.execute(AnswerTarget(numbers={"answer_chars": 120, "answer_ms": 5}))
    assert outcome.verdict == "ok"


@pytest.mark.parametrize("policy", ["failures", "all"])
async def test_no_screenshot_of_an_ai_try_that_showed_answer_text(policy):
    harness = Harness(options=replace(OPTIONS, screenshots=policy))
    outcome = await harness.execute(AnswerTarget(numbers={"answer_chars": 10}))
    assert outcome.error == "no_answer"
    [(record, shot)] = harness.recorder.tries
    assert shot is None and record["screenshot"] is None and record["reason"] == "short_answer"


async def test_an_ai_try_without_answer_text_keeps_its_failure_screenshot():
    harness = Harness()
    await harness.execute(AnswerTarget(numbers={"answer_chars": 0}))
    assert harness.recorder.tries[0][1] == b"jpeg"


async def test_answer_text_only_hides_screenshots_of_ai_sources():
    harness = Harness()
    await harness.execute(AnswerTarget(id="browser_site", numbers={"answer_chars": 10}))
    assert harness.recorder.tries[0][1] == b"jpeg"


BAD_NUMBERS = [{"#content": 1}, {"answer_chars": 1.5}, {"answer_chars": True}]


@pytest.mark.parametrize("numbers", BAD_NUMBERS)
async def test_interact_numbers_must_be_ints_not_named_like_a_selector(numbers):
    harness = Harness()
    outcome = await harness.execute(AnswerTarget(numbers=numbers))
    reason = harness.recorder.tries[0][0]["reason"]
    assert (outcome.error, reason) == ("browser_error", "valueerror")
