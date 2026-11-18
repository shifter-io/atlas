"""The cross-check fixture of the all-targets bill (VALUE-ALL-RANK-1): the same per-target
metrics priced at the same standard pages (VALUE-FAIL-1: the median wire bytes per try of two
of the cases, "pages") by the calculation (calculated/value_all.py) and by the site API's billOver
(api/internal/build/value_all.go, TestValueAllCrossCheck), which must agree to 1e-9.

Run `uv run python tests/value_all_crosscheck.py` to rewrite the fixture after a change;
tests/test_value_all.py fails while the file differs from what this makes."""

import json
import sys
from pathlib import Path

from atlas.calculated.policy import load
from atlas.calculated.value_all import bill, card_targets, priced, standard_pages, target_costs

FIXTURE = Path(__file__).parent.parent / "api/internal/build/testdata/value_all_crosscheck.json"


def _metrics(completed, successes, up, down):
    return {"completed": completed, "successes": successes, "bytes_up": up, "bytes_down": down}


def _figures(price, metrics, config):
    return target_costs(price, {t: {"metrics": m} for t, m in metrics.items()}, config)


def _case(name, price, metrics, config, pages):
    cost, partial, counted, missing = bill(priced(price, _figures(price, metrics, config), pages))
    return {
        "name": name,
        "price": price,
        "metrics": metrics,
        "expected": {"cost": cost, "partial": partial, "counted": counted, "missing": missing},
    }


def fixture():
    """Every case's metrics and the calculation's bill."""
    config, _ = load("configs/methodology.toml")
    cards = card_targets(config)
    full = {
        t: _metrics(4000 + 37 * i, 3100 - 211 * i, 1.25e6 * (i + 1), 9.7e8 + 3.3e7 * i)
        for i, t in enumerate(cards)
    }
    zero = full | {cards[3]: _metrics(800, 0, 2.1e5, 4.4e7), cards[6]: _metrics(90, 0, 9e3, 0)}
    holes = full | {cards[1]: _metrics(0, 0, None, None), cards[4]: _metrics(50, 12, 0, 0)}
    big = {t: _metrics(123457, 98765 - i, 3.2e9 + i, 4.1e10 - 7 * i) for i, t in enumerate(cards)}
    cases = [
        ("complete", 0.75, full),
        ("no successes on two targets: partial", 1.75, zero),
        ("no price", None, full),
        ("no run and no wire bytes", 2.2, holes),
        ("large windows", 3.98, big),
    ]
    # The median of two members is their mean, so no case is priced at its own bytes.
    pages = standard_pages([_figures(0.75, full, config), _figures(3.98, big, config)])
    return {
        "targets": cards,
        "pages": pages,
        "cases": [_case(name, price, m, config, pages) for name, price, m in cases],
    }


def text():
    return json.dumps(fixture(), indent=1) + "\n"


if __name__ == "__main__":
    if sys.argv[1:] == ["--print"]:
        sys.stdout.write(text())
    else:
        FIXTURE.write_text(text())
