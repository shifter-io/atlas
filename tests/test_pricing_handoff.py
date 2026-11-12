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
    previous = [
        h for h in nodemaven.splitlines() if h.startswith("('nodemaven','residential','2026-11-11'")
    ]
    assert len(previous) == 1 and ",4.25,2.2," in previous[0]

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
