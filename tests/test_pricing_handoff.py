"""scripts/pricing_handoff.py (#927): a pricing check's rows become a network's catalog SQL and
price evidence under the #927 rules, and a second run writes the same files."""

import importlib.util
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "pricing_handoff", ROOT / "scripts" / "pricing_handoff.py"
)
handoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handoff)


def row(
    slug, plan, gb, billing, regular, paid, source="none", notes="", product="Residential rotating"
):
    return {
        "slug": slug,
        "product": product,
        "plan": plan,
        "gb_num": gb,
        "billing": billing,
        "regular_month": regular * gb,
        "discounted_month": paid * gb,
        "regular_per_gb": regular,
        "discounted_per_gb": paid,
        "discount_pct": round(100 - 100 * paid / regular, 1),
        "discount_source": source,
        "notes": notes,
        "url": f"https://{slug}.example/pricing",
    }


CHECK = {
    "providers": [
        {"slug": s, "pricing_url": f"https://{s}.example/pricing"}
        for s in ("nodemaven", "proxyempire")
    ],
    "rows": [
        row(
            "nodemaven",
            "PAYG 2 GB",
            2,
            "PAYG one-time",
            5.0,
            2.5,
            "coupon BF26 shown on page (2x traffic)",
        ),
        row(
            "nodemaven",
            "Billed monthly 2 GB",
            2,
            "monthly",
            4.25,
            2.125,
            "coupon BF26 shown on page (2x traffic)",
        ),
        row(
            "nodemaven",
            "Billed monthly 1000 GB",
            1000,
            "monthly",
            2.2,
            1.1,
            "coupon BF26 shown on page (2x traffic)",
        ),
        row("nodemaven", "ISP 1", 1, "30 days", 9.0, 9.0, product="ISP (static residential)"),
        row(
            "proxyempire",
            "Pay As You Go 1 GB",
            1,
            "PAYG one-time",
            7.0,
            3.5,
            "coupon EMPIRE50 auto-applied (50%)",
        ),
        row(
            "proxyempire",
            "Enterprise",
            1000,
            "monthly",
            3.0,
            1.5,
            "coupon EMPIRE50 auto-applied (50%)",
        ),
        row("proxyempire", "Custom Plans", 5000, "custom", 0.75, 0.75),
    ],
}


def test_rules_history_and_rerun(tmp_path, monkeypatch):
    for sub in ("api/scripts", "configs/pricing"):
        (tmp_path / sub).mkdir(parents=True)
    for slug in ("nodemaven", "proxyempire"):
        shutil.copy(ROOT / "api" / "scripts" / f"pricing-{slug}.sql", tmp_path / "api" / "scripts")
    check = tmp_path / "pricing.json"
    check.write_text(json.dumps(CHECK))
    monkeypatch.setattr(handoff, "ROOT", tmp_path)
    handoff.main(str(check), "2026-11-12")
    first = {p.name: p.read_text() for p in tmp_path.rglob("*-*.*")}
    handoff.main(str(check), "2026-11-12")
    assert first == {p.name: p.read_text() for p in tmp_path.rglob("*-*.*")}

    nodemaven = first["pricing-nodemaven.sql"]
    # A typed code is not applied: list prices, pay as you go first, the code as the note.
    assert "'per_gb',5.0,2.2,10.0," in nodemaven
    assert "'PAYG 2 GB',10.0,2.0,NULL,'BF26',false,NULL," in nodemaven
    assert "ISP 1" not in nodemaven
    assert (
        "('nodemaven','residential','2026-11-12',5.0,2.2,'https://nodemaven.example/pricing',false)"
        in nodemaven
    )
    # #940: the history starts at the first true check; no old-catalog row comes back.
    assert nodemaven.count("('nodemaven','residential','2026-") == 1
    # A later check keeps the committed true checks and adds its own.
    handoff.main(str(check), "2026-12-01")
    later = (tmp_path / "api" / "scripts" / "pricing-nodemaven.sql").read_text()
    assert "('nodemaven','residential','2026-11-12',5.0,2.2," in later
    assert "('nodemaven','residential','2026-12-01',5.0,2.2," in later

    # #987: the trial's numbers stay as committed, each plan has its audience and its pool's
    # kinds, and no size or period words.
    assert "'Paid trial: $3.50 for 750 MB',3.5,0.0,0.75,NULL,NULL,false)" in nodemaven
    assert "trial_needs_card=EXCLUDED.trial_needs_card" in nodemaven
    assert '"poolKinds": ["residential"], ' in nodemaven and '"audience": "all"' in nodemaven
    assert '"bandwidth"' not in nodemaven and '"period"' not in nodemaven

    empire = first["pricing-proxyempire.sql"]
    # An automatic coupon counts; a custom offer is left out.
    assert "'per_gb',3.5,1.5,3.5," in empire and "Custom Plans" not in empire
    assert '"discountSource": "auto_coupon"' in empire and '"code": "EMPIRE50"' in empire
    evidence = json.loads(first["proxyempire-residential.json"])
    assert {e["metric"]: e["value"] for e in evidence} == {
        "price_per_gb_lowest": 1.5,
        "price_per_gb_entry": 3.5,
        "price_per_gb_basis": 1.5,
        "price_basis_plan_gb": 1000,
    }
    assert all(
        e["period_start"] == "2026-11-08T00:00:00Z" and "observed 2026-11-12" in e["source"]
        for e in evidence
    )


def test_a_plan_without_a_size_is_the_entry_only_when_pay_as_you_go():
    """A monthly rate without a size is never the entry (a custom quote is left out before, #960);
    SOAX's Sandbox (pay as you go, no size) is the entry."""
    payg_1gb = {"gb": 1.0, "billing": "payg", "rate": 3.98, "row": {"product": "x"}}
    volume = {"gb": None, "billing": "monthly", "rate": 2.2, "row": {"product": "x"}}
    entry, lowest = handoff.rates([volume, payg_1gb], "evomi")
    assert entry is payg_1gb and lowest is volume
    sandbox = {"gb": None, "billing": "payg", "rate": 5.0, "row": {"product": "x"}}
    team = {"gb": 227.3, "billing": "monthly", "rate": 2.2, "row": {"product": "x"}}
    assert handoff.rates([team, sandbox], "soax")[0] is sandbox


def basis_of(*rows):
    """basis() over check rows of one network: (plan name, GB, rate, kind)."""
    plan, kind = handoff.basis(handoff.plans(list(rows), rows[0]["slug"]), rows[0]["slug"])
    return plan["row"]["plan"], plan["gb"], plan["rate"], kind


def test_the_basis_is_the_first_plan_of_200_gb_or_more():
    """#956: the smallest plan of at least 200 GB; at that size the plan listed first on the
    page, not the cheaper one (#960); a typed code is not applied."""
    code = "coupon BF26 shown on page (2x traffic)"
    assert basis_of(
        row("nodemaven", "PAYG 200 GB", 200, "PAYG one-time", 3.25, 1.625, code),
        row("nodemaven", "Billed monthly 200 GB", 200, "monthly", 2.85, 1.425, code),
        row("nodemaven", "Billed monthly 1000 GB", 1000, "monthly", 2.2, 1.1, code),
        row("nodemaven", "Billed monthly 100 GB", 100, "monthly", 3.0, 1.5, code),
    ) == ("PAYG 200 GB", 200, 3.25, "first_200gb_plus")
    # Only 241 GB qualifies; the annual plan of that size and the custom quote do not count.
    assert basis_of(
        row("infatica", "100 GB monthly", 100, "monthly", 3.6, 3.6),
        row("infatica", "241 GB monthly", 241, "monthly", 2.9, 2.9),
        row("infatica", "2892 GB annual", 241, "annual, shown as monthly equivalent", 2.9, 2.32),
        row("infatica", "Custom", 5000, "custom", 0.5, 0.5),
    ) == ("241 GB monthly", 241, 2.9, "first_200gb_plus")


def test_without_a_plan_of_200_gb_the_basis_is_the_largest_plan():
    """No plan of 200 GB or more: the largest plan with a stated GB; a plan without a size
    (Evomi Premium's volume rate) cannot qualify."""
    assert basis_of(
        row("massive", "Scaler", 70, "monthly", 5.5, 5.5),
        row("massive", "Pro", 100, "monthly", 4.9, 4.9),
        row("massive", "Enterprise", 1000, "custom", 3.0, 3.0),
    ) == ("Pro", 100, 4.9, "largest_below_200gb")
    volume = row("evomi", "Volume", 1, "monthly", 2.2, 2.2, product="Premium residential")
    volume["gb_num"] = None
    payg = row("evomi", "Pay as you go", 1, "PAYG", 3.98, 3.98, product="Premium residential")
    assert basis_of(payg, volume) == ("Pay as you go", 1, 3.98, "largest_below_200gb")


def test_at_equal_size_the_first_listed_plan_wins():
    """#960: the subscription listed before pay as you go wins, even though it is cheaper only
    in one of the two orders."""
    sub = row("geonode", "250 GB subscription", 250, "monthly", 0.66, 0.594, "subscription")
    payg = row("geonode", "250 GB pay as you go", 250, "PAYG one-time", 0.55, 0.55)
    assert basis_of(sub, payg) == ("250 GB subscription", 250, 0.594, "first_200gb_plus")
    assert basis_of(payg, sub) == ("250 GB pay as you go", 250, 0.55, "first_200gb_plus")


IPROYAL = [
    row("iproyal", "1GB subscription", 1, "monthly subscription", 7.35, 7.0, "subscription"),
    row("iproyal", "1GB pay as you go", 1, "PAYG one-time", 7.35, 7.35),
    row("iproyal", "10GB subscription", 10, "monthly subscription", 5.51, 5.25, "subscription"),
    row("iproyal", "10GB pay as you go", 10, "PAYG one-time", 5.51, 5.51),
    row("iproyal", "Custom plans (from 10 TB) subscription", 10000, "monthly", 1.84, 1.75),
    row("iproyal", "Custom plans (from 10 TB) pay as you go", 10000, "PAYG one-time", 1.84, 1.84),
]


def test_a_custom_tier_never_counts():
    """#960: a custom or quoted tier is not a published plan: not a plan, not the lowest rate
    and not the basis. IPRoyal falls back to its largest published plan, the 10 GB
    subscription listed first."""
    ps = handoff.plans(IPROYAL, "iproyal")
    assert [p["row"]["plan"] for p in ps] == [r["plan"] for r in IPROYAL[:4]]
    entry, lowest = handoff.rates(ps, "iproyal")
    assert (entry["rate"], lowest["rate"]) == (7.35, 5.25)
    assert basis_of(*IPROYAL) == ("10GB subscription", 10, 5.25, "largest_below_200gb")
    # A quote with no plan size (Evomi Premium's volume rate) is custom too.
    volume = row("evomi", "Volume", 1, "monthly", 2.2, 2.2, notes="as low as $2.20/GB; get a quote")
    volume["gb_num"] = None
    assert handoff.custom(volume)


def test_a_published_plan_is_not_custom():
    """A word inside another ("new customers") or a quote only beyond the largest plan
    ("500+ GB custom quote") leaves a published plan in."""
    intro = row("dataimpulse", "Intro (new customers, one-time)", 5, "PAYG one-time", 1.0, 1.0)
    fleet = row("fleetproxy", "500 GB", 500, "PAYG", 1.59, 1.59, notes="500+ GB custom quote")
    assert not handoff.custom(intro) and not handoff.custom(fleet)


def test_pool_kinds_and_audience():
    assert handoff.pool_kinds("Residential + mobile rotating") == ["residential", "mobile"]
    assert handoff.pool_kinds("Residential, Full Geo pool") == ["residential"]
    assert handoff.pool_kinds("Full Geo") == ["residential"]
    assert handoff.audience("Intro (new customers, one-time)") == "new_customers"
    assert handoff.audience("Starter") == "all"


def test_a_script_without_trial_numbers_gets_none(tmp_path, monkeypatch):
    (tmp_path / "api" / "scripts").mkdir(parents=True)
    sql = (ROOT / "api" / "scripts" / "pricing-proxyempire.sql").read_text()
    old = sql.replace("',1.97,0.0,0.15,NULL,NULL,false)", "',false)")
    (tmp_path / "api" / "scripts" / "pricing-proxyempire.sql").write_text(old)
    monkeypatch.setattr(handoff, "ROOT", tmp_path)
    prior = handoff.previous("proxyempire", "2026-12-01")
    assert prior["trial"] == "$1.97 trial, 150 MB, no expiry"
    assert prior["trial_numbers"] == handoff.NO_TRIAL_NUMBERS
