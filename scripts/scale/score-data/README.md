# Test list prices (VALUE-TEST-1, #839)

**Test data.** The list prices of the tested networks as the Shifter website's review pages
state them (`src/lib/reviews/<network>.ts` of the website, branch `proxy-index`), not verified
against the networks' own pricing pages. They let the Index Score's value component and the
pages' price figures be filled on the two test days in `proxy_reviews`.

| File | What it is |
|---|---|
| `price-evidence.json` | 2 `metric_evidence` records per network: `price_per_gb_lowest` (the smallest numeric per-GB rate of the review's tiers; "on request" and "Custom" are skipped, a "~" rate is used and its source says it is approximate) and `price_per_gb_entry` (the first tier's rate). Cohort `"*"`, from 2026-10-01 to 2027-01-01. |
| `catalog-prices.sql` | One transaction, as the catalog writer role, after `siteapi migrate`: per network the `site.providers` display name (real, not test data), `site.pricing` (`per_gb`, entry and at-scale rate, the "Free trial" fact) and one `site.pricing_plans` row per tier, whose `catalog.source` starts with `TEST DATA`. |
| `prices.py` | Makes both files: `uv run python scripts/scale/score-data/prices.py <website checkout>`. It prints the per-network table and names any network it left out. |

Shifter is not in these files: its verified seed (`configs/pricing/shifter-residential.json`)
and catalog (`api/scripts/pricing-shifter.sql`) stay as they are. Geonode has no review page on
the website, so it has no price here. Bright Data has a review page but is not a tested network.

## Loading (#840)

```sh
psql <site database, catalog writer role> -f scripts/scale/score-data/catalog-prices.sql
proxy-index metric-evidence scripts/scale/score-data/price-evidence.json   # + the production guard
proxy-index metric-evidence configs/pricing/shifter-residential.json
```

Both files can be loaded again: the catalog is upserted and evidence with the same content is
ignored.

## Before the website goes live

`clean_target.py` keeps the whole site schema, so these catalog rows survive the launch clean-up.
Replace or delete the test catalog prices (`catalog.source` starts with `TEST DATA`) before the
website goes live; the price evidence goes with the rest of the test data.
