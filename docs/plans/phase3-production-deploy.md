# Phase 3 production deploy

The full checklist (schema migrations, the calculation, the siteapi and its settings, checks and
rollback) is #812's. This file starts with the launch to-do items other issues record.

## Launch to-do

- **Catalog prices (#839).** The list prices in `api/scripts/pricing-<slug>.sql` and
  `configs/pricing/<slug>-residential.json` were read on each network's own pricing page, with
  the page URL and capture date in every row (`catalog.source`, `catalog.checkedAt`,
  `site.pricing_history.source`). These verified rows stay. Only the rows whose `catalog.source`
  starts with `TEST DATA` (a figure the network's own page did not give, taken from the Shifter
  website's review page) must be verified on the network's site or removed before the website
  goes live. `clean_target.py` keeps the whole site schema, so the launch clean-up leaves them in
  place; the price evidence in `metric_evidence` goes with the rest of the test data and is
  reloaded with `proxy-index metric-evidence configs/pricing/<slug>-residential.json`.
