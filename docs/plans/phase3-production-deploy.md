# Phase 3 production deploy

The full checklist (schema migrations, the calculation, the siteapi and its settings, checks and
rollback) is #812's. This file starts with the launch to-do items other issues record.

Every siteapi deploy, the production one included, follows
the operator's deploy runbook (kept outside the repository): the build on a disposable server,
the database steps before the switch (migrations, catalog loads, the grants check), the blue/green
switch and the checks after it.

## Launch to-do

- **Catalog prices (#839).** The list prices in `api/scripts/pricing-<slug>.sql` and
  `configs/pricing/<slug>-residential.json` were read on each network's own pricing page, with
  the page URL and capture date in every row (`catalog.source`, `catalog.checkedAt`,
  `site.pricing_history.source`). These verified rows stay. Only the rows whose `catalog.source`
  starts with `TEST DATA` (a figure the network's own page did not give, taken from the Shifter
  website's review page) must be verified on the network's site or removed before the website
  goes live. `clean_target.py` keeps the whole site schema, so the launch clean-up leaves them in
  place; the price evidence in `metric_evidence` goes with the rest of the test data and is
  reloaded with `atlas metric-evidence configs/pricing/<slug>-residential.json`.
- **Generated trust figures (#862).** `scripts/scale/score-data/trust-test-data.sql` wrote modelled
  blocklist hit rates, sticky hold rates and leak checks for the two test days: no framework test
  measures them, and every leak check it wrote carries `"note": "TEST DATA (generated, not
  measured)"`. `clean_target.py` keeps the site schema, so delete Part B's generated trust rows
  (`trust-test-data.sql`) before the website goes live; these fields return to `not_measured`:
  `UPDATE site.trust_signals SET blacklist_hit_rate = NULL, stickiness_hold_rate = NULL,
  leak_checks = NULL, updated_at = now() WHERE leak_checks @> '[{"note": "TEST DATA (generated,
  not measured)"}]';`. The facts in the same rows (`api/scripts/catalog-network-facts.sql`: pool
  claim, KYC, consent, advertised stickiness) were read on each network's own site and stay.
- **Profile strengths and weaknesses (#962): done.** `profile.strengths`, `profile.weaknesses` and
  the rest of the English profile copy (tagline, claim, verdict, schedule) are no longer served
  (api/FIELDS.md, "Removed on 2026-11-13 (prose)").
- **Scope codes (#966).** Rerun `api/scripts/catalog-scopes.sql` on the production database once
  (idempotent; `site.scopes` only): until then `/v1/meta` `scopes[].geoCoverage.code` is null.
