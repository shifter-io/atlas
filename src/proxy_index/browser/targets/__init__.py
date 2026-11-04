"""The browser targets of K-BP1: the five page targets (pages) and, later, the AI sources.

A page target builds one page per try from its settings inputs, rotating through them, and
judges what the browser saw with browser.judge. They are registered in proxy_index.targets.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from ...models import RequestContext
from ...targets import BrowserTarget, PageSpec, PageView
from ..judge import Mark, PageAssessment, judge, selector_list


def choose(context: RequestContext, inputs: Sequence[Any]) -> Any:
    """The input of try context.request_index: inputs[i % n], so a run rotates its list."""
    if not inputs:
        raise ValueError("inputs: a browser target needs at least one input")
    return inputs[context.request_index % len(inputs)]


@dataclass(frozen=True, slots=True)
class PageTarget(BrowserTarget):
    """A browser target with a page per input: address() is its URL, wanted() the selectors
    the transport waits for and counts into PageView.found, rules() the pass condition that
    runs after the shared steps and the site's MARKS."""

    MARKS: ClassVar[tuple[Mark, ...]] = ()
    BLOCK_STATUSES: ClassVar[frozenset[int]] = frozenset()
    SETTLE_MS: ClassVar[int] = 0

    def page(self, context: RequestContext, inputs: tuple[Any, ...]) -> PageSpec:
        value = choose(context, inputs)
        return PageSpec(self.address(value), selector_list(*self.wanted(value)), self.SETTLE_MS)

    def try_fields(self, context: RequestContext, inputs: tuple[Any, ...]) -> dict[str, Any]:
        """The try record's `input` and `source` for this try: page targets have no source."""
        return {"input": choose(context, inputs), "source": None}

    def judge(self, view: PageView) -> PageAssessment:
        return judge(view, self.rules, self.MARKS, self.BLOCK_STATUSES)

    def address(self, value: Any) -> str:
        raise NotImplementedError

    def wanted(self, value: Any) -> tuple[str, ...]:
        raise NotImplementedError

    def rules(self, view: PageView) -> PageAssessment:
        raise NotImplementedError
