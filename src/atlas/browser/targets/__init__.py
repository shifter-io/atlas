"""The browser targets of K-BP1: the five page targets (pages) and, later, the AI sources.

A page target builds one page per try from its settings inputs, rotating through them, and
judges what the browser saw with browser.judge. They are registered in atlas.targets.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from ...models import RequestContext
from ...targets import BrowserTarget, PageSpec, PageView
from ..contracts import PROXY_AUTH_STATUS
from ..judge import OK, Mark, PageAssessment, judge, selector_list


def choose(context: RequestContext, inputs: Sequence[Any]) -> Any:
    """The input of try context.request_index: inputs[i % n], so a run rotates its list."""
    if not inputs:
        raise ValueError("inputs: a browser target needs at least one input")
    return inputs[context.request_index % len(inputs)]


@dataclass(frozen=True, slots=True)
class PageTarget(BrowserTarget):
    """A browser target with a page per input: address() is its URL, wanted() the selectors
    the transport waits for and counts into PageView.found, rules() the pass condition that
    runs after the shared steps and the site's MARKS, and shown() whether the page shows the
    target's own content (see judge())."""

    MARKS: ClassVar[tuple[Mark, ...]] = ()
    BLOCK_STATUSES: ClassVar[frozenset[int]] = frozenset()
    SETTLE_MS: ClassVar[int] = 0
    # What the try waits for instead of any one of wanted(): a page whose parts arrive one by
    # one (a profile's header before its posts) is only read once it is whole, or shows a
    # failure. Empty: any of wanted(), as before.
    READY: ClassVar[tuple[str, ...]] = ()
    # Once shown(): the PageView fields blank for the marks, markers and rules (see judge()),
    # or with SHOWN_OK an ok right after the 407 step.
    SHOWN_UNREAD: ClassVar[tuple[str, ...]] = ("title", "text_head")
    SHOWN_OK: ClassVar[bool] = False

    def page(self, context: RequestContext, inputs: tuple[Any, ...]) -> PageSpec:
        value = choose(context, inputs)
        wanted, ready = selector_list(*self.wanted(value)), selector_list(*self.READY)
        return PageSpec(self.address(value), wanted, self.SETTLE_MS, ready=ready or None)

    def try_fields(self, context: RequestContext, inputs: tuple[Any, ...]) -> dict[str, Any]:
        """The try record's `input` and `source` for this try: page targets have no source."""
        return {"input": choose(context, inputs), "source": None}

    def judge(self, view: PageView) -> PageAssessment:
        """browser.judge with this target's rules, MARKS and BLOCK_STATUSES. A page that
        shows the target's own content (shown()) holds that content's words, so they are no
        challenge or block page: its SHOWN_UNREAD fields are blank for the marks, the shared
        markers and the rules, while statuses and marks by path or selector still count. With
        SHOWN_OK such a page is ok right after the 407 step, before any other shared step."""
        if self.shown(view):
            if self.SHOWN_OK and view.status != PROXY_AUTH_STATUS:
                return OK
            view = replace(view, **dict.fromkeys(self.SHOWN_UNREAD, ""))
        return judge(view, self.rules, self.MARKS, self.BLOCK_STATUSES)

    def shown(self, view: PageView) -> bool:
        """Whether the page shows the target's own content (a profile, an answer, the
        expected text), never a challenge or block page; by default no page does."""
        return False

    def address(self, value: Any) -> str:
        raise NotImplementedError

    def wanted(self, value: Any) -> tuple[str, ...]:
        raise NotImplementedError

    def rules(self, view: PageView) -> PageAssessment:
        raise NotImplementedError
