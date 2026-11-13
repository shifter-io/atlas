"""Write api/scripts/pricing-<slug>.sql and configs/pricing/<slug>-residential.json from a
pricing check's pricing.json (#927). The check itself stays outside the repository: its path
is given at run time. Usage: pricing_handoff.py <pricing.json> <YYYY-MM-DD>

Rules (#927): one product per network; the price a buyer pays without typing a code (a typed
code is a plan note, an automatic discount counts); entry = the smallest paid plan's rate
(pay as you go before a subscription of the same size); lowest = the lowest published rate,
custom offers left out. basis (#956, the rate every cost uses) = the smallest plan of at least
200 GB, monthly or pay as you go, the cheaper at that size (annual commitments and custom quotes
left out); without one, the largest plan with a stated GB."""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTS = {
    "shifter": "Residential, Full Geo pool",
    "evomi": "Premium residential",  # the product the tests ran on (owner, 2026-11-13)
    "rayobyte": "Premium residential",
    "massive": "Residential rotating",
}
OWN = {"soax": lambda r: "Tier 1" in r["plan"]}  # tier 1 (main markets) is the comparable rate
EXCLUDED = ("Static", "ISP", "IPv6", "Unlimited", "Premium residential", "Scale", "Enterprise")
# The architect's reading (#927): Geonode's 100 TB rate is also in the pricing file the page links.
NOTES = {
    "geonode": " Larger tiers also in https://geonode.com/pricing/residential-proxies.md, "
    "the pricing file the page links (last verified 2026-08-05)."
}
COMMITMENT = ("payg", "30_day", "monthly", "annual")
TEST_DAYS = "2026-11-08T00:00:00Z"  # the first test day in proxy_reviews (#847)
PERIODS = {"monthly": "/month", "annual": "/month", "30_day": "/30 days"}


def billing(text):
    t = text.lower()
    if "annual" in t or "yearly" in t:
        return "annual"
    if "30-day" in t or "30 days" in t:
        return "30_day"
    if "payg" in t or "pay as you go" in t or "one-time" in t:
        return "payg"
    return "monthly" if "month" in t else t


def discount(source):
    s = (source or "").lower()
    for key, code in (
        ("automatic sale", "sale"),
        ("auto-applied", "auto_coupon"),
        ("annual billing", "annual_billing"),
        ("subscription", "subscription"),
        ("volume", "volume"),
    ):
        if key in s:
            return code
    return None


def typed_code(row):
    """A code the buyer must type: not applied, kept as the plan's note."""
    if (row["discount_source"] or "").startswith("coupon") and "auto-applied" not in row[
        "discount_source"
    ]:
        return re.search(r"coupon (\w+)", row["discount_source"]).group(1)
    found = re.search(r"code ([A-Z0-9]{4,})", row["notes"] or "")
    return found.group(1) if found and "applied automatically" not in row["notes"] else None


def plans(rows, slug):
    want = PRODUCTS.get(slug)
    keep = [
        r
        for r in rows
        if r["slug"] == slug
        and r["billing"] != "custom"
        and (r["product"] == want if want else not any(x in r["product"] for x in EXCLUDED))
        and OWN.get(slug, lambda r: True)(r)
        and (r["regular_per_gb"] or 0) > 0
        and r["discount_source"] != "free credit"
    ]
    if slug == "shifter":  # all three pools stay on the page; the rates are the Full Geo pool's
        keep = [r for r in rows if r["slug"] == slug]
    out = []
    for r in keep:
        code = typed_code(r)
        typed = code and (r["discount_source"] or "").startswith("coupon")
        per_gb = r["regular_per_gb"] if typed else r["discounted_per_gb"]
        price = r["regular_month"] if typed else r["discounted_month"]
        out.append(
            {
                "row": r,
                "code": code,
                "rate": per_gb,
                "price": price,
                "pct": None if typed or not r["discount_pct"] else r["discount_pct"],
                "source": None if typed else discount(r["discount_source"]),
                "billing": billing(r["billing"]),
                "gb": r["gb_num"],
            }
        )
    return out


def rates(plans_, slug):
    priced = [p for p in plans_ if slug != "shifter" or p["row"]["product"] == PRODUCTS["shifter"]]
    # A plan without a size is pay as you go from the first GB (SOAX's Sandbox), or a volume
    # plan for the largest buyers (Evomi Premium's "as low as" rate), never the smallest.
    entry = min(
        priced,
        key=lambda p: (
            p["gb"] if p["gb"] is not None else (0 if p["billing"] == "payg" else float("inf")),
            COMMITMENT.index(p["billing"]) if p["billing"] in COMMITMENT else 9,
        ),
    )
    lowest = min(priced, key=lambda p: (p["rate"], p["gb"] or 0))
    return entry, lowest


BASIS_GB = 200
BASIS_BILLING = ("payg", "30_day", "monthly")


def basis(plans_, slug):
    """(the basis plan, its kind): the first plan of BASIS_GB or more, else the largest plan
    with a stated GB (#956). Only monthly (30-day included) and pay-as-you-go plans count."""
    sized = [
        p
        for p in plans_
        if p["gb"] is not None
        and p["billing"] in BASIS_BILLING
        and (slug != "shifter" or p["row"]["product"] == PRODUCTS["shifter"])
    ]
    big = [p for p in sized if p["gb"] >= BASIS_GB]
    if big:
        size = min(p["gb"] for p in big)
        kind = "first_200gb_plus"
    else:
        size = max(p["gb"] for p in sized)
        kind = "largest_below_200gb"
    return min((p for p in sized if p["gb"] == size), key=lambda p: p["rate"]), kind


def bandwidth(gb):
    if gb is None:
        return None
    return f"{gb / 1000:g} TB" if gb >= 1000 else f"{gb:g} GB"


PLAN = re.compile(
    r"\('[^']+','residential',\d+,'((?:[^']|'')*)',[\d.]+,[\d.]+,NULL,"
    r"(NULL|'(?:[^']|'')*'),(true|false),[^,]+,'(\{.*?\})'::jsonb,false\)"
)


def previous(slug, today):
    """The committed figures: name, trial, the earlier true checks' history rows, and the plans
    (Shifter keeps each offer's description, features, order URL, note and flag)."""
    sql = (ROOT / "api" / "scripts" / f"pricing-{slug}.sql").read_text()
    name = re.search(r"VALUES \('[^']+','((?:[^']|'')*)','", sql).group(1)
    trial = re.search(r"'per_gb',[\d.]+,[\d.]+,[\d.NUL]+,'((?:[^']|'')*)',false\)", sql).group(1)
    history = re.findall(r"\('[^']+','residential','([\d-]+)',([\d.]+),([\d.]+),'(\S+?)[ ']", sql)
    # The true checks already committed stay (#940: the history starts at the first true check).
    checks = sorted((d, float(e), float(s), u) for d, e, s, u in history if d < today)
    old = {}
    for plan, note, flag, catalog in PLAN.findall(sql):
        c = json.loads(catalog.replace("''", "'"))
        old[c["pool"], c.get("tier"), plan] = (note, flag == "true", c)
    return {
        "name": name.replace("''", "'"),
        "trial": trial.replace("''", "'"),
        "checks": checks,
        "plans": old,
    }


def offer(p, slug, day, old):
    """A plan's name, note, recommended flag and catalog."""
    r = p["row"]
    c = {
        "pool": r["product"],
        "unitRate": p["rate"],
        "originalPrice": r["regular_month"],
        "originalUnitRate": r["regular_per_gb"],
        "bandwidth": bandwidth(p["gb"]),
        "billing": p["billing"],
        "period": PERIODS.get(p["billing"], "one-time"),
        "discountSource": p["source"],
        "url": r["url"],
        "source": r["url"],
        "checkedAt": day,
    }
    if p["source"] == "auto_coupon":
        c["code"] = re.search(r"coupon (\w+)", r["discount_source"]).group(1)
    name, note = r["plan"], lit(p["code"])
    popular = "popular" in (r["notes"] or "").lower()
    if slug == "shifter":
        pool = re.search(r"Residential, (.+) pool", r["product"]).group(1)
        tier, _, rest = name.partition(" ")
        tier, name = (tier, rest) if rest else (None, tier)
        note, popular, prior = old["plans"][pool, tier, name]
        c = {**prior, **c, "pool": pool, "tier": tier, "url": prior["url"]}
    return name, note, popular, c


def lit(v):
    if v is None:
        return "NULL"
    return (
        repr(float(v))
        if isinstance(v, (int, float)) and not isinstance(v, bool)
        else "'" + str(v).replace("'", "''") + "'"
    )


PRICING = "provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo"
PLANS = (
    "provider_slug,category,position,name,price,gb,ips,note,"
    "recommended,discount_pct,catalog,is_demo"
)
HISTORY = "provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo"
PRICING_SET = (
    "ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,"
    "price_per_gb=EXCLUDED.price_per_gb,"
)


def sql(net, rows, entry, lowest):
    """The network's catalog transaction, in the #847 pattern: the earlier true checks' history
    rows and this check's."""
    slug, name, url, day, old = (net[k] for k in ("slug", "name", "url", "day", "old"))
    values = []
    for i, (plan, note, popular, p, c) in enumerate(rows):
        values.append(
            f"('{slug}','residential',{i},{lit(plan)},{lit(p['price'])},{lit(p['gb'])},NULL,"
            f"{note},{str(popular).lower()},{lit(p['pct'])},{lit(json.dumps(c))}::jsonb,false)"
        )
    hist = [*old["checks"], (day, entry["rate"], lowest["rate"], url)]
    sep = ",\n"
    history = sep.join(
        f"('{slug}','residential','{d}',{lit(e)},{lit(s)},{lit(u)},false)" for d, e, s, u in hist
    )
    pricing = ",".join(
        [f"'{slug}','residential','per_gb'", lit(entry["rate"]), lit(lowest["rate"])]
        + [lit(entry["price"]), lit(old["trial"]), "false"]
    )
    where = f"provider_slug='{slug}' AND category='residential'"
    return f"""-- {name} residential list prices, read on {url} on {day}.
-- Product: {net["product"]}. Written by scripts/pricing_handoff.py (#927).
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only {name}'s residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('{slug}',{lit(name)},'{slug}')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing({PRICING})
VALUES ({pricing})
{PRICING_SET}
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE {where} AND position>={len(rows)};
INSERT INTO site.pricing_plans({PLANS}) VALUES
{sep.join(values)}
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history({HISTORY}) VALUES
{history}
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
"""


def evidence(net, entry, lowest, base):
    """The value input. The test days' builds have as-of 2026-11-10, so the evidence keeps
    #847's period_start 2026-11-08: with equal dates the newer evidence_id wins (FIELDS.md).
    base is basis(): its rate and plan GB price every cost (#956)."""
    slug, url, day, extra = (net[k] for k in ("slug", "url", "day", "extra"))
    plan, kind = base

    def record(metric, p, label, value=None):
        return {
            "provider": slug,
            "product": "residential",
            "cohort": "*",
            "metric": metric,
            "value": p["rate"] if value is None else value,
            "period_start": net["start"],
            "period_end": "2027-01-01T00:00:00Z",
            "valid_until": "2027-01-01T00:00:00Z",
            "source": f"{url} observed {day}: {net['product']}. {label} ${p['rate']:g}/GB: "
            f"{p['row']['plan']} ({p['billing']}).{extra if p is lowest else ''}",
        }

    return [
        record("price_per_gb_lowest", lowest, "Lowest rate"),
        record("price_per_gb_entry", entry, "Entry rate"),
        record("price_per_gb_basis", plan, f"Cost basis ({kind}) rate"),
        record(
            "price_basis_plan_gb",
            plan,
            f"Cost basis ({kind}) plan of {plan['gb']:g} GB at",
            plan["gb"],
        ),
    ]


def main(path, day):
    data = json.loads(Path(path).read_text())
    for provider in data["providers"]:
        slug, url = provider["slug"], provider["pricing_url"]
        old, ps = previous(slug, day), plans(data["rows"], slug)
        entry, lowest = rates(ps, slug)
        product = PRODUCTS.get(slug) or entry["row"]["product"]
        rows = [(*offer(p, slug, day, old), p) for p in ps]
        rows = [(n, note, pop, p, c) for n, note, pop, c, p in rows]
        file = ROOT / "configs" / "pricing" / f"{slug}-residential.json"
        net = {
            "slug": slug,
            "name": old["name"],
            "url": url,
            "product": product,
            "day": day,
            "old": old,
            "extra": NOTES.get(slug, ""),
            "start": TEST_DAYS,
        }
        (ROOT / "api" / "scripts" / f"pricing-{slug}.sql").write_text(sql(net, rows, entry, lowest))
        file.write_text(json.dumps(evidence(net, entry, lowest, basis(ps, slug)), indent=2) + "\n")
        print(
            f"{slug}: {lowest['rate']:g}-{entry['rate']:g}, {len(rows)} plans, "
            f"history {[c[0] for c in old['checks']] + [net['day']]}"
        )


if __name__ == "__main__":
    main(*sys.argv[1:])
