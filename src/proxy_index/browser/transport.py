"""BrowserTransport: the runner's Transport for browser targets (docs/browser.html, Runner).

One execute() is one try: lease a pooled browser, open a fresh context through the try's
proxy (in the run country's locale, time zone and geolocation), navigate, settle, let the
target interact, read the PageView, judge it, hand the try record to the recorder and return
the Outcome the runner counts like an HTTP request.

Bytes (BYTES-TRUTH-1): the context's proxy is the try's local relay (relay.py), which forwards to
the gateway and counts both ways, so `bytes_up` and `bytes_down` are the wire bytes of the whole
try, exact (`bytes_exact`): the CONNECT or SOCKS5 exchange, TLS, headers, uploads, failed
sub-requests and aborted loads included. A try cut at its deadline or by a browser error keeps
what crossed before; a gateway that refused the connection records 0. `bytes_down_est`, the summed
body sizes the browser reported, stays in tries.jsonl as a cross-check; only a try that never
opened its relay (SOCKS5 with credentials, refused) falls back to it, inexact.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import re
import time
import uuid
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..bytes_io import ByteCount
from ..models import Outcome, RequestContext, RunConfig
from ..targets import BrowserTarget, PageSpec, PageView
from .contracts import (
    AI_SOURCES,
    GOOGLE_SEARCH,
    TIMINGS,
    TOKEN,
    bundle_members,
    classify_navigation_error,
    try_record,
)
from .driver import NavigationError, Page, UserAgentRefused
from .geo import GeoRefused
from .judge import PageAssessment, failed, selectors
from .pool import BrowserPool, EnginePool
from .relay import relayed
from .settings import BrowserOptions, TargetSettings
from .targets.ai import ANSWER_CHARS

log = logging.getLogger(__name__)

TEXT_HEAD = 4096
# Google's consent interstitial and its accept control; accepted once per try's context.
CONSENT_HOST = re.compile(r"consent\.google\.[a-z.]+")
CONSENT_ACCEPT = 'button#L2AGLb, form[action*="consent.google"] button'


class Recorder(Protocol):
    """Receives every try: its validated tries.jsonl record and its screenshot, if taken."""

    def try_done(self, record: dict[str, Any], screenshot: bytes | None) -> None: ...


class NullRecorder:
    """The default recorder: keeps nothing (BP-DATA supplies the file recorder)."""

    def try_done(self, record: dict[str, Any], screenshot: bytes | None) -> None:
        return None


def _ms(since: float) -> int:
    return round((time.perf_counter() - since) * 1000)


def _exception_reason(exc: BaseException) -> str:
    """A browser_error try's reason: the exception's class name, lower-cased."""
    name = type(exc).__name__.lower()
    return name if re.fullmatch(TOKEN, name) else "exception"


def _reason(name: str | None) -> str:
    lowered = (name or "").lower()
    return lowered if re.fullmatch(TOKEN, lowered) else "navigation_error"


def _numbers(extra: Any, found: dict[str, int]) -> dict[str, int]:
    """interact()'s named numbers, added to PageView.found after the selector counts; a name
    that is also a selector, or a value that is not an int, is refused (browser_error)."""
    numbers = dict(extra or {})
    if set(numbers) & set(found) or any(type(value) is not int for value in numbers.values()):
        raise ValueError("interact() returned a selector's name or a value that is not an int")
    return numbers


def _page_url(url: str) -> str | None:
    parts = urlsplit(url)
    return url if parts.scheme in ("http", "https") and parts.hostname else None


async def _accept_consent(page: Page, target: Any) -> None:
    """One click on accept when the consent page or form hides the results (a banner beside
    the results is left alone, as GoogleSearch judges it)."""
    host = urlsplit(await page.url()).hostname or ""
    hidden = await page.count(target.CONSENT_FORM) and not await page.count(target.RESULTS)
    # Without the accept control there is nothing to click: the judge reads the consent page.
    if (CONSENT_HOST.fullmatch(host) or hidden) and await page.count(CONSENT_ACCEPT):
        await page.click(CONSENT_ACCEPT)


class BrowserTransport:
    """A run's browser tries on the host's shared pool, or on one engine's pool of it (then
    every try record names that `engine`). `settings` is the target's [[target]]:
    its inputs, and its timeout_s and block_resources overriding the [browser] defaults.
    `view_sink`, if given, receives (request index, the PageView judge() got) after the try is
    recorded, once per try that reached a view; only scripts/browser_capture.py passes one."""

    def __init__(
        self,
        config: RunConfig,
        *,
        pool: BrowserPool | EnginePool,
        browser: BrowserOptions,
        settings: TargetSettings,
        recorder: Recorder | None = None,
        deadline: float | None = None,
        view_sink: Callable[[int, PageView], None] | None = None,
    ) -> None:
        self.config, self.pool, self.deadline = config, pool, deadline
        self.engine = pool.name if isinstance(pool, EnginePool) else None
        self.view_sink = view_sink
        self.recorder = recorder if recorder is not None else NullRecorder()
        self.inputs = settings.inputs
        self.timeout_s = settings.timeout_s or browser.page_timeout_s
        blocked = settings.block_resources
        self.block = browser.block_resources if blocked is None else blocked
        self.screenshots = browser.screenshots
        self._run_id, self._index = str(uuid.uuid4()), 0

    @classmethod
    def factory(
        cls, pool: BrowserPool | EnginePool, recorder: Recorder | None = None, **options: Any
    ) -> Callable[[RunConfig], BrowserTransport]:
        """The runner's transport_factory; `options` are browser, settings and deadline."""
        return functools.partial(cls, pool=pool, recorder=recorder, **options)

    async def __aenter__(self) -> BrowserTransport:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def _budget(self) -> asyncio.Timeout:
        """Cancels an in-flight try at the deadline plus one page timeout. Under runner.run
        the runner cancels its tries at the deadline itself; this is the backstop for any
        other caller, and for a transport given a deadline its runner does not have."""
        if self.deadline is None:
            return asyncio.timeout(None)
        remaining = self.deadline + self.timeout_s - time.time()
        return asyncio.timeout_at(asyncio.get_running_loop().time() + remaining)

    async def execute(
        self, target: BrowserTarget, *, context: RequestContext | None = None
    ) -> Outcome:
        if context is None:
            context = RequestContext(self._run_id, self._index, 0)
            self._index += 1
        began = time.perf_counter()
        timings: dict[str, int | None] = dict.fromkeys(TIMINGS)
        # The try's facts as its steps learn them (timings_ms, main status, URL, bytes, shot).
        state: dict[str, Any] = {"timings": timings, "status": None, "final_url": None}
        state |= {"bytes": None, "shot": None, "found": {}, "view": None, "fingerprint": None}
        state["relay"] = None
        budget = self._budget()
        try:
            async with budget:
                assessment = await self._attempt(target, context, state)
        except (UserAgentRefused, GeoRefused):
            raise
        except Exception as exc:  # boundary: a failed try is recorded, never raised
            if isinstance(exc, TimeoutError) and budget.expired():
                assessment = failed("timeout", "deadline")
            else:
                # A crash or unexpected exception; the pool replaces a disconnected browser.
                log.warning("Browser try failed (%s)", type(exc).__name__)
                assessment = failed("browser_error", _exception_reason(exc))
        timings["total"] = _ms(began)
        relay = state["relay"]
        wire = relay.count if relay is not None else ByteCount(0, state["bytes"] or 0, False)
        outcome = Outcome(
            latency_ms=float((timings["navigate"] or 0) + (timings["settle"] or 0)),
            error=assessment.error,
            status=state["status"],
            verdict=assessment.verdict,
            bytes_up=wire.up,
            bytes_down=wire.down,
            bytes_exact=wire.exact,
            completed_at=time.perf_counter(),
        )
        self._record(target, context, outcome, getattr(assessment, "reason", None), state)
        # After the record, so a sink changes neither the try's timings nor its record.
        if self.view_sink is not None and state["view"] is not None:
            self.view_sink(context.request_index, state["view"])
        return outcome

    async def _attempt(self, target: BrowserTarget, context: RequestContext, state: dict) -> Any:
        timings = state["timings"]
        proxy = self.config.proxy.resolve(context)
        if proxy.scheme == "socks5" and proxy.username is not None:
            network = (self.config.provider, self.config.product)
            if network not in self.pool.refused_networks:
                self.pool.refused_networks.add(network)
                log.warning("Browser tries refuse SOCKS5 with credentials (%s/%s)", *network)
            return failed("proxy_connect", "socks5_auth_unsupported")
        spec = target.page(context, self.inputs)
        mark = time.perf_counter()
        async with self.pool.browser() as browser, relayed(proxy) as (through, relay):
            timings["queue"] = _ms(mark)
            # Read once the relay has closed, after the context: whatever crossed, however the
            # try ends.
            state["relay"] = relay
            mark = time.perf_counter()
            timeout_ms, country = self.timeout_s * 1000, self.config.requested_country
            async with browser.context(through, self.block, timeout_ms, country=country) as page:
                timings["context"] = _ms(mark)
                return await self._on_page(page, target, spec, state)

    async def _on_page(self, page: Page, target: BrowserTarget, spec: PageSpec, state: dict) -> Any:
        # First, so a try that fails in any way still records its fingerprint (a Chrome try's).
        state["fingerprint"] = getattr(page, "fingerprint", None)
        try:
            assessment = await self._load(page, target, spec, state)
        except NavigationError as exc:
            code, verdict = classify_navigation_error(exc.name)
            assessment = PageAssessment(error=code, verdict=verdict, reason=_reason(exc.name))
        state["final_url"] = _page_url(await page.url())
        state["bytes"] = await page.bytes_down_est()
        failure = assessment.error is not None
        # Privacy: a screenshot of an AI source that showed answer text would hold the answer.
        answered = target.id in AI_SOURCES and state["found"].get(ANSWER_CHARS, 0) > 0
        policy = self.screenshots == "all" or (failure and self.screenshots == "failures")
        if policy and not answered:
            with contextlib.suppress(Exception):
                state["shot"] = await page.screenshot()
        return assessment

    async def _load(self, page: Page, target: BrowserTarget, spec: PageSpec, state: dict) -> Any:
        """Navigate, then settle: consent, wait_for, settle_ms and the target's interact()."""
        timings = state["timings"]
        timeout_ms = self.timeout_s * 1000
        mark = time.perf_counter()
        state["status"] = await page.goto(spec.url, timeout_ms)
        timings["navigate"] = _ms(mark)
        mark = time.perf_counter()
        if target.id == GOOGLE_SEARCH:
            await _accept_consent(page, target)
        if spec.wait_for:
            await page.wait_for(spec.wait_for, timeout_ms)
        await asyncio.sleep(spec.settle_ms / 1000)
        extra = await target.interact(page, spec)
        timings["settle"] = _ms(mark)
        found = {selector: await page.count(selector) for selector in selectors(spec.wait_for)}
        found |= _numbers(extra, found)
        state["found"] = found
        view = PageView(
            final_url=await page.url(),
            status=state["status"],
            title=await page.title(),
            text_head=(await page.text("body"))[:TEXT_HEAD],
            found=found,
            timings_ms=dict(timings),
        )
        state["view"] = view
        return target.judge(view)

    def _record(
        self,
        target: BrowserTarget,
        context: RequestContext,
        outcome: Outcome,
        reason: str | None,
        state: dict,
    ) -> None:
        i, shot = context.request_index, state["shot"]
        fingerprint = state["fingerprint"]
        record = try_record(
            i=i,
            **target.try_fields(context, self.inputs),
            verdict=outcome.verdict,
            error=outcome.error,
            status=outcome.status,
            final_url=state["final_url"],
            timings_ms=state["timings"],
            bytes_down_est=state["bytes"],
            reason=reason,
            screenshot=bundle_members().screenshot(i) if shot is not None else None,
            **({"fingerprint": fingerprint} if fingerprint is not None else {}),
            **({"engine": self.engine} if self.engine is not None else {}),
        )
        self.recorder.try_done(record, shot)
