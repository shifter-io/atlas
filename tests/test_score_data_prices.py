"""VALUE-TEST-1 (#839): the test list prices made from the website's review pages."""

import importlib.util
import json
from datetime import datetime
from pathlib import Path

from proxy_index.calculated.aggregate import EVIDENCE_RANGES, PRICES

DATA = Path(__file__).resolve().parents[1] / "scripts" / "scale" / "score-data"
spec = importlib.util.spec_from_file_location("score_data_prices", DATA / "prices.py")
prices = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prices)

ALPHA = """import type { ReviewData } from './index';
export const data: ReviewData = {
  slug: 'alpha',
  name: 'Alpha\\'s Proxies',
  facts: [
    { label: 'Entry price', value: '$9 for 3 GB' },
    { label: 'Free trial', value: '1 day' },
  ],
  pricing: [
    { tier: 'Entry', traffic: '3 GB', perGb: '$3.00', monthly: '$9' },
    { tier: 'Mid', traffic: '~1 TB', perGb: '~$1.50', monthly: '~$1,500' },
    { tier: 'Volume', traffic: '10 TB+', perGb: 'on request', monthly: 'on request' },
    { tier: 'Top', traffic: 'Any', perGb: 'Custom', monthly: 'Contact sales' },
  ],
};
"""
KEYS = ("provider", "product", "cohort", "metric", "value")
KEYS += ("period_start", "period_end", "valid_until", "source")
EMPTY = """export const data = {
  name: 'Beta',
  pricing: [
    { tier: 'Only', traffic: 'Any', perGb: 'on request', monthly: 'on request' },
  ],
};
"""


def website(tmp_path):
    reviews = tmp_path / "src" / "lib" / "reviews"
    reviews.mkdir(parents=True)
    (reviews / "alpha.ts").write_text(ALPHA)
    (reviews / "beta.ts").write_text(EMPTY)
    return tmp_path


def test_rates_and_volumes():
    assert prices.money("$2,000.50") == (2000.5, False)
    assert prices.money("~$2.20") == (2.2, True)
    assert prices.money("on request") is prices.money("from $32.50") is None
    assert prices.volume("2 TB") == 2000 and prices.volume("750 MB") == 0.75
    assert prices.volume("~100 GB") == 100 and prices.volume("1 TB+") is None


def test_lowest_skips_requests_and_entry_is_the_first_tier(tmp_path):
    page = prices.review(website(tmp_path) / "src/lib/reviews/alpha.ts")
    assert page["name"] == "Alpha's Proxies" and page["facts"]["Free trial"] == "1 day"
    lowest, entry = prices.prices(page)
    assert lowest[0] == (1.5, True) and lowest[1]["tier"] == "Mid"
    assert entry[0] == (3.0, False) and entry[1]["tier"] == "Entry"
    record = prices.evidence("alpha", page, lowest, entry)[0]
    assert record["source"].startswith(prices.LABEL + ': Alpha\'s Proxies review, tier "Mid"')
    assert record["source"].endswith("Approximate: the page gives ~$1.50/GB.")


def test_main_writes_both_files_and_names_what_it_left_out(tmp_path, capsys, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(prices, "tested", lambda: ["alpha", "beta", "gamma"])
    prices.main([str(website(tmp_path)), "--out", str(out)])
    records = json.loads((out / "price-evidence.json").read_text())
    assert [(r["provider"], r["metric"], r["value"]) for r in records] == [
        ("alpha", "price_per_gb_lowest", 1.5),
        ("alpha", "price_per_gb_entry", 3.0),
    ]
    sql = (out / "catalog-prices.sql").read_text()
    assert sql.count("BEGIN;") == sql.count("COMMIT;") == 1
    assert "'Alpha''s Proxies'" in sql and "'per_gb',3.0,1.5,'1 day',false" in sql
    assert "('alpha','residential',1,'Mid',1500.0,1000.0," in sql
    assert "('alpha','residential',3,'Top',NULL,NULL," in sql
    assert "position>=4" in sql and str(tmp_path) not in sql + json.dumps(records)
    shown = capsys.readouterr().out
    assert (
        "| Alpha's Proxies | `src/lib/reviews/alpha.ts` | ~$1.50 (Mid) | $3.00 (Entry) | 4 |"
        in shown
    )
    assert "beta: the review has no numeric tier" in shown
    assert "gamma: no review page on the website" in shown


def test_the_committed_evidence_prices_every_reviewed_network():
    records = json.loads((DATA / "price-evidence.json").read_text())
    networks = {p.stem for p in prices.FORMULAS.glob("*.json")} - prices.KEEP - {"geonode"}
    assert {(r["provider"], r["metric"]) for r in records} == {
        (n, m) for n in networks for m in PRICES
    }
    for r in records:
        assert set(r) == {*KEYS}
        assert r["product"] == "residential" and r["cohort"] == "*"
        assert r["source"].startswith(prices.LABEL + ": ")
        low, high = EVIDENCE_RANGES[r["metric"]]
        assert low < r["value"] <= high
        start, end = (datetime.fromisoformat(r[k]) for k in ("period_start", "valid_until"))
        assert start.isoformat() == "2026-10-01T00:00:00+00:00" and end == datetime.fromisoformat(
            r["period_end"]
        )
    by = {(r["provider"], r["metric"]): r["value"] for r in records}
    assert all(by[n, "price_per_gb_lowest"] <= by[n, "price_per_gb_entry"] for n in networks)


def test_every_committed_plan_is_marked_test_data():
    sql = (DATA / "catalog-prices.sql").read_text()
    plans = [line for line in sql.splitlines() if line.startswith("('")]
    assert len(plans) == 55 and all("false,NULL,'{" in p for p in plans)
    source = f'"source": "{prices.LABEL}: '
    assert all(source in p and p.endswith(("::jsonb,false),", "::jsonb,false)")) for p in plans)
