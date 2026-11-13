-- The five test scopes of /v1/meta and the dataset's scopes[] (site.scopes), in the website's
-- order, as codes and booleans (META-SCOPES-CODES-1, #966): the website writes the words. The
-- countries and cadence numbers come from the snapshot and the test configuration, not from here.
-- geo_coverage is a code: all_usable_countries, provider_inventory or restricted. summary is the
-- owner's approved sentence that only /v1/networks/{slug} profile.protocol.summary serves; the
-- label, cadence and rationale texts are cleared. Every scope runs every target (all_targets, no
-- target_ids). A rerun updates the rows in place.
BEGIN;

INSERT INTO site.scopes(product_kind, summary, live_ip_sampling, pool_tracking, overlap_audit, stickiness_test,
                        geo_coverage, sort_order, label, cadence, rationale, all_targets, target_ids, is_demo)
SELECT v.*, NULL, NULL, NULL, true, '{}'::text[], false
FROM (VALUES
  ('residential_rotating', 'A new exit IP per request or per short session, drawn from a shared pool.',
   true, true, true, false, 'all_usable_countries', 10),
  ('residential_sticky', 'A residential exit held for an advertised TTL, for multi-step sessions.',
   true, true, true, true, 'all_usable_countries', 20),
  ('isp_static', 'A dedicated IP on an eyeball-network ASN, rented monthly.',
   false, false, true, false, 'provider_inventory', 30),
  ('datacenter', 'A dedicated or shared IP on a hosting ASN.',
   false, false, true, false, 'provider_inventory', 40),
  ('mobile', 'Carrier-assigned exits behind CGNAT, shared with real subscribers.',
   true, true, true, true, 'restricted', 50)
) AS v(product_kind, summary, live_ip_sampling, pool_tracking, overlap_audit, stickiness_test, geo_coverage, sort_order)
ON CONFLICT (product_kind) DO UPDATE
SET summary = EXCLUDED.summary, label = NULL, cadence = NULL, rationale = NULL,
    live_ip_sampling = EXCLUDED.live_ip_sampling, pool_tracking = EXCLUDED.pool_tracking,
    overlap_audit = EXCLUDED.overlap_audit, stickiness_test = EXCLUDED.stickiness_test,
    geo_coverage = EXCLUDED.geo_coverage, sort_order = EXCLUDED.sort_order,
    all_targets = EXCLUDED.all_targets, target_ids = EXCLUDED.target_ids, is_demo = false, updated_at = now();

COMMIT;
