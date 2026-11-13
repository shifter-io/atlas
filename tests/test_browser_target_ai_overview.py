"""Google AI Overview (BP-AI-TARGETS; docs/browser.html, "What counts as success").

Its fixture cases run with the other sources' in test_browser_targets_ai.py.
"""

from test_browser_targets_ai import EXAMPLE, context

from atlas.browser import contracts as c
from atlas.browser.settings import load_settings
from atlas.targets import registry


def test_overview_rotates_its_questions():
    target = registry.get(c.AI_OVERVIEW)
    questions = ("why is the sky blue", "how do rainbows form", "what is a tide")
    for i in range(7):
        spec = target.page(context(i), questions)
        query = questions[i % 3].replace(" ", "+")
        assert spec.url == f"https://www.google.com/search?q={query}&hl=en&gl=us"
        assert target.try_fields(context(i), questions) == {
            "input": questions[i % 3],
            "source": c.AI_OVERVIEW,
        }
    settings = load_settings(EXAMPLE)
    overview = next(t for t in settings.targets if t.id == c.AI_OVERVIEW)
    assert overview.answer_wait_s == 30


def test_the_overview_shares_google_search_selectors_marks_and_url():
    overview, search = registry.get(c.AI_OVERVIEW), registry.get(c.GOOGLE_SEARCH)
    for name in ("RESULTS", "CAPTCHA_FORM", "CONSENT_FORM", "MARKS"):
        assert getattr(overview, name) is getattr(search, name)
    for query in ("why is the sky blue", "a & b"):
        assert overview.address(query) == search.address(query)
