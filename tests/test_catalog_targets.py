"""The site catalog's targets (api/scripts/catalog-targets.sql, CATALOG-TARGETS-SCOPES-1): one row
per framework target, in targets.toml order, and each browser target's difficulty the
methodology's [browser_difficulty] (a group's its hardest source's), so the published difficulty
and the scoring weight never disagree."""

import re
import tomllib
from pathlib import Path

from proxy_index.calculated.policy import browser_difficulty, load

CATALOG = Path("api/scripts/catalog-targets.sql")
ROW = re.compile(r"^\s*\('(\w+)', '([^']+)', (\d), (\d+)\),?$", re.MULTILINE)


def catalog():
    """The catalog rows: [(target, category, difficulty, sort_order)]."""
    return [(t, c, int(d), int(o)) for t, c, d, o in ROW.findall(CATALOG.read_text())]


def test_catalog_lists_every_framework_target_in_order():
    targets = [t["id"] for t in tomllib.loads(Path("configs/targets.toml").read_text())["target"]]
    rows = catalog()
    assert [r[0] for r in rows] == targets
    assert [r[3] for r in rows] == sorted({r[3] for r in rows})  # distinct, ascending


def test_browser_difficulty_is_the_methodology_weight():
    weights = browser_difficulty(load(Path("configs/methodology.toml"))[0])
    rows = {t: d for t, _, d, _ in catalog()}
    assert weights and {t: rows[t] for t in weights} == weights
    assert all(1 <= d <= 5 for d in rows.values())
    assert rows["ai_answers"] == max(weights[s] for s in ("ai_overview", "ai_chatgpt", "ai_gemini"))
