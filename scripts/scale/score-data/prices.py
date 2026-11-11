"""VALUE-TEST-1: list prices from the website's review pages, as test data (#839).

  uv run python scripts/scale/score-data/prices.py <website checkout>

Reads src/lib/reviews/<network>.ts of the website checkout (branch proxy-index) for every
tested network (formulas/*.json) except Shifter, whose verified seed and catalog stay as they
are, and writes price-evidence.json and catalog-prices.sql next to this file. Prints the table
for the pull request. The website path is never written into either file.
"""

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORMULAS = HERE.parents[2] / "formulas"
REVIEWS = Path("src/lib/reviews")
KEEP = {"shifter"}  # verified seed and catalog: configs/pricing, api/scripts/pricing-shifter.sql
LABEL = "TEST DATA (Shifter website review page, not verified)"
PERIOD = ("2026-10-01T00:00:00Z", "2027-01-01T00:00:00Z")
CATEGORY = "residential"
KEYS = ("tier", "traffic", "perGb", "monthly")
STRING = r"'((?:[^'\\]|\\.)*)'"
NAME = re.compile(r"^  name:\s*" + STRING, re.M)
FACT = re.compile(r"\{\s*label:\s*" + STRING + r",\s*value:\s*" + STRING + r"\s*\}")
TABLE = re.compile(r"^  pricing: \[(.*?)^  \],", re.M | re.S)
TIER = re.compile(r"\{\s*" + r",\s*".join(f"{k}:\\s*{STRING}" for k in KEYS) + r",?\s*\}")
MONEY = re.compile(r"^(~)?\$([\d,]+(?:\.\d+)?)$")
VOLUME = re.compile(r"^(~)?([\d,]+(?:\.\d+)?) (MB|GB|TB)$")
GB = {"MB": 0.001, "GB": 1, "TB": 1000}


def text(raw):
    return re.sub(r"\\(.)", r"\1", raw)


def money(value):
    """(amount, approximate) of "$7.00" or "~$2.50"; None for "on request", "Custom" or ranges."""
    m = MONEY.match(value.strip())
    return (float(m[2].replace(",", "")), bool(m[1])) if m else None


def volume(value):
    """GB of "10 GB", "2 TB" or "~100 GB"; None for "Any", ranges and "1 TB+"."""
    m = VOLUME.match(value.strip())
    return float(m[2].replace(",", "")) * GB[m[3]] if m else None


def review(path):
    """The display name, facts and tiers of one review file."""
    source = path.read_text()
    name, table = NAME.search(source), TABLE.search(source)
    if not name or not table:
        raise ValueError(f"{path.name}: no name or pricing table")
    tiers = [{k: text(t[i]) for i, k in enumerate(KEYS)} for t in TIER.findall(table[1])]
    return {
        "name": text(name[1]),
        "facts": {text(k): text(v) for k, v in FACT.findall(source)},
        "tiers": tiers,
    }


def prices(page):
    """The lowest and the entry per-GB rate, each with its tier (None if not numeric)."""
    rated = [(money(t["perGb"]), t) for t in page["tiers"]]
    numeric = [(r, t) for r, t in rated if r]
    lowest = min(numeric, key=lambda rt: rt[0][0]) if numeric else None
    entry = rated[0] if rated and rated[0][0] else None
    return lowest, entry


def source(page, tier):
    rate, approximate = money(tier["perGb"])
    note = f" Approximate: the page gives {tier['perGb']}/GB." if approximate else ""
    return (
        f'{LABEL}: {page["name"]} review, tier "{tier["tier"]}" ({tier["traffic"]}) at '
        f"${rate:.2f}/GB.{note}"
    )


def evidence(network, page, lowest, entry):
    return [
        {
            "provider": network,
            "product": CATEGORY,
            "cohort": "*",
            "metric": metric,
            "value": rate[0],
            "period_start": PERIOD[0],
            "period_end": PERIOD[1],
            "valid_until": PERIOD[1],
            "source": source(page, tier),
        }
        for metric, (rate, tier) in (("price_per_gb_lowest", lowest), ("price_per_gb_entry", entry))
    ]


def sql(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def plan(network, page, position, tier):
    rate, price = money(tier["perGb"]), money(tier["monthly"])
    catalog = {
        "bandwidth": tier["traffic"],
        "unitRate": rate[0] if rate else None,
        "perGbText": tier["perGb"],
        "monthlyText": tier["monthly"],
        "approximate": any(t and t[1] for t in (rate, price)),
        "source": f'{LABEL}: {page["name"]} review, tier "{tier["tier"]}"',
    }
    gb = volume(tier["traffic"])
    row = (network, CATEGORY, position, tier["tier"], price and price[0], gb, None, None, False)
    return "(" + ",".join(map(sql, row)) + f",NULL,{sql(json.dumps(catalog))}::jsonb,false)"


def catalog(network, page, lowest, entry):
    """The network's site.providers name, site.pricing row and site.pricing_plans rows."""
    slug, plans = sql(network), page["tiers"]
    rows = ",\n".join(plan(network, page, i, t) for i, t in enumerate(plans))
    trial = sql(page["facts"].get("Free trial"))
    pricing = f"{slug},{sql(CATEGORY)},'per_gb',{sql(entry[0][0])},{sql(lowest[0][0])},{trial}"
    return f"""-- {page["name"]}: {REVIEWS / network}.ts
INSERT INTO site.providers(slug,name,framework_provider)
VALUES ({slug},{sql(page["name"])},{slug})
ON CONFLICT (slug) DO UPDATE SET name=EXCLUDED.name,framework_provider=EXCLUDED.framework_provider,
 updated_at=now();
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,trial,
 is_demo)
VALUES ({pricing},false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 trial=EXCLUDED.trial,is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans
WHERE provider_slug={slug} AND category={sql(CATEGORY)} AND position>={len(plans)};
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,
 discount_pct,catalog,is_demo) VALUES
{rows}
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
"""


HEADER = f"""-- VALUE-TEST-1 (#839): {LABEL}.
-- Generated by scripts/scale/score-data/prices.py from the website's review pages; do not edit.
-- Run as the catalog writer role, after siteapi migrate. Shifter's catalog is not touched
-- (api/scripts/pricing-shifter.sql). Replace or delete these rows before the website goes live:
-- every plan's catalog.source starts with "TEST DATA".
BEGIN;
"""


def row(network, page, lowest, entry):
    """One line of the pull request's table."""

    def cell(rt):
        return f"{rt[1]['perGb']} ({rt[1]['tier']})" if rt else "none"

    return (
        f"| {page['name']} | `{REVIEWS / network}.ts` | {cell(lowest)} | {cell(entry)} | "
        f"{len(page['tiers'])} | {page['facts'].get('Free trial', 'not stated')} |"
    )


def build(website, networks):
    """(evidence records, catalog SQL, table lines, networks left out with the reason)."""
    records, statements, lines, missing = [], [HEADER], [], []
    for network in networks:
        path = website / REVIEWS / f"{network}.ts"
        if not path.is_file():
            missing.append((network, "no review page on the website"))
            continue
        page = review(path)
        lowest, entry = prices(page)
        if not lowest or not entry:
            missing.append((network, "the review has no numeric tier"))
            continue
        records += evidence(network, page, lowest, entry)
        statements.append(catalog(network, page, lowest, entry))
        lines.append(row(network, page, lowest, entry))
    return records, "\n".join(statements) + "COMMIT;\n", lines, missing


def tested():
    return sorted(p.stem for p in FORMULAS.glob("*.json") if p.stem not in KEEP)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("website", type=Path, help="the website checkout (branch proxy-index)")
    parser.add_argument("--out", type=Path, default=HERE, help="where the two files are written")
    args = parser.parse_args(argv)
    records, statements, lines, missing = build(args.website, tested())
    (args.out / "price-evidence.json").write_text(json.dumps(records, indent=2) + "\n")
    (args.out / "catalog-prices.sql").write_text(statements)
    print("| Network | Website file | Lowest $/GB (tier) | Entry $/GB (tier) | Plans | Trial |")
    print("|---|---|---|---|---|---|")
    print("\n".join(lines))
    for network, why in missing:
        print(f"\nLeft out: {network}: {why}.")
    print(f"\n{len(records)} evidence records, {len(lines)} networks.", file=sys.stderr)


if __name__ == "__main__":
    main()
