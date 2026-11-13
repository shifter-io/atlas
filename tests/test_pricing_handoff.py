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

    empire = first["pricing-proxyempire.sql"]
    # An automatic coupon counts; a custom offer is left out.
    assert "'per_gb',3.5,1.5,3.5," in empire and "Custom Plans" not in empire
    assert '"discountSource": "auto_coupon"' in empire and '"code": "EMPIRE50"' in empire
    evidence = json.loads(first["proxyempire-residential.json"])
    assert {e["metric"]: e["value"] for e in evidence} == {
        "price_per_gb_lowest": 1.5,
        "price_per_gb_entry": 3.5,
    }
    assert all(
        e["period_start"] == "2026-11-08T00:00:00Z" and "observed 2026-11-12" in e["source"]
        for e in evidence
    )


def test_a_plan_without_a_size_is_the_entry_only_when_pay_as_you_go():
    """Evomi Premium's volume rate has no size: it is the lowest, never the entry; SOAX's
    Sandbox (pay as you go, no size) is the entry."""
    payg_1gb = {"gb": 1.0, "billing": "payg", "rate": 3.98, "row": {"product": "x"}}
    volume = {"gb": None, "billing": "monthly", "rate": 2.2, "row": {"product": "x"}}
    entry, lowest = handoff.rates([volume, payg_1gb], "evomi")
    assert entry is payg_1gb and lowest is volume
    sandbox = {"gb": None, "billing": "payg", "rate": 5.0, "row": {"product": "x"}}
    team = {"gb": 227.3, "billing": "monthly", "rate": 2.2, "row": {"product": "x"}}
    assert handoff.rates([team, sandbox], "soax")[0] is sandbox
