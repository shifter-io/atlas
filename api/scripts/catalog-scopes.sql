-- The five test scopes of /v1/meta and the dataset's scopes[] (site.scopes), in the website's
-- order. Every scope runs every target (all_targets, no target_ids). A rerun updates the rows
-- in place.
-- Served (#966): the booleans and geo_coverage, a code (all_usable_countries,
-- provider_inventory or restricted); the API adds the targets, countries and cadence numbers.
-- Internal only: label and summary feed /v1/networks/{slug} profile.protocol.
-- Not served: the cadence and rationale texts, kept as the methodology's record.
-- The texts are the website's src/data/proxy-index/methodology.json scopes, copied verbatim:
-- the owner's approved copy, so change them only with the owner.
BEGIN;

INSERT INTO site.scopes(product_kind, label, summary, cadence, live_ip_sampling, pool_tracking, overlap_audit,
                        stickiness_test, geo_coverage, rationale, sort_order, all_targets, target_ids, is_demo)
SELECT v.*, true, '{}'::text[], false
FROM (VALUES
  ('residential_rotating', 'Rotating residential',
   'A new exit IP per request or per short session, drawn from a shared pool.',
   'Continuous, 5-minute buckets', true, true, true, false,
   'all_usable_countries',
   'This is the product most buyers are actually choosing between, and the one where claims are least checkable. It gets the full suite: every target, continuous exit-IP sampling, churn tracking and the cross-network overlap audit.',
   10),
  ('residential_sticky', 'Sticky residential',
   'A residential exit held for an advertised TTL, for multi-step sessions.',
   'Continuous, 5-minute buckets', true, true, true, true,
   'all_usable_countries',
   'Same suite as rotating, plus TTL verification: we hold a session for its full advertised duration and record whether the exit IP survived. Advertised TTLs are ceilings, and the gap is never disclosed.',
   20),
  ('isp_static', 'Static ISP',
   'A dedicated IP on an eyeball-network ASN, rented monthly.',
   'Hourly', false, false, true, false,
   'provider_inventory',
   'These IPs do not rotate, so churn, pool size and live sampling are meaningless. What matters instead is reputation drift over time, per-IP rate limits and whether the ASN really is an eyeball network. The overlap audit still runs, because the same subnet is often resold by several vendors.',
   30),
  ('datacenter', 'Datacenter',
   'A dedicated or shared IP on a hosting ASN.',
   'Hourly', false, false, true, false,
   'provider_inventory',
   'Static, cheap and easy to test, so cadence is lower and the live machinery is switched off. Blocklist presence is tracked closely instead, because it is what actually determines whether these work.',
   40),
  ('mobile', 'Mobile',
   'Carrier-assigned exits behind CGNAT, shared with real subscribers.',
   'Continuous, 15-minute buckets', true, true, true, true,
   'restricted',
   'Mobile bandwidth costs an order of magnitude more than residential, so a full geographic sweep is not affordable. The country set is deliberately restricted and buckets are wider, which means mobile pool figures are a floor rather than a census. That trade-off is stated here rather than buried.',
   50)
) AS v(product_kind, label, summary, cadence, live_ip_sampling, pool_tracking, overlap_audit,
       stickiness_test, geo_coverage, rationale, sort_order)
ON CONFLICT (product_kind) DO UPDATE
SET label = EXCLUDED.label, summary = EXCLUDED.summary, cadence = EXCLUDED.cadence,
    live_ip_sampling = EXCLUDED.live_ip_sampling, pool_tracking = EXCLUDED.pool_tracking,
    overlap_audit = EXCLUDED.overlap_audit, stickiness_test = EXCLUDED.stickiness_test,
    geo_coverage = EXCLUDED.geo_coverage, rationale = EXCLUDED.rationale, sort_order = EXCLUDED.sort_order,
    all_targets = EXCLUDED.all_targets, target_ids = EXCLUDED.target_ids, is_demo = false, updated_at = now();

COMMIT;
