-- TEST DATA (generated, not measured). For the two test days only.
-- No framework test measures a network's blocklist hit rate, sticky-session
-- hold rate or leak checks (api/FIELDS.md, "not measured by the framework").
-- The website needs values to lay out its pages, so this file writes modelled
-- ones for the 18 measured networks: a blocklist hit rate from 0.2% to 4%
-- (no two equal), a sticky hold rate from 0.80 to 0.99, and five leak checks
-- that pass except for one failed check on three networks.
-- Every leak check carries "note": "TEST DATA (generated, not measured)", a
-- key the API does not serve; it marks each row this file writes.
-- It writes only blacklist_hit_rate, stickiness_hold_rate and leak_checks of
-- site.trust_signals: the advertised catalog facts in the same row
-- (api/scripts/catalog-network-facts.sql) stay. Before the website goes live,
-- clear these three columns again (docs/plans/phase3-production-deploy.md).
BEGIN;

WITH v(provider, blacklist_hit_rate, stickiness_hold_rate, failed_check) AS (VALUES
  ('byteful', 0.021, 0.88, NULL::text),
  ('dataimpulse', 0.034, 0.84, 'dns'),
  ('decodo', 0.006, 0.95, NULL),
  ('evomi', 0.012, 0.91, NULL),
  ('fleetproxy', 0.027, 0.86, NULL),
  ('geonode', 0.038, 0.82, 'webrtc'),
  ('infatica', 0.016, 0.89, NULL),
  ('iproyal', 0.009, 0.93, NULL),
  ('massive', 0.031, 0.85, NULL),
  ('nodemaven', 0.004, 0.94, NULL),
  ('oxylabs', 0.002, 0.97, NULL),
  ('proxy-cheap', 0.040, 0.80, 'ipv6'),
  ('proxy-seller', 0.036, 0.83, NULL),
  ('proxyempire', 0.024, 0.87, NULL),
  ('rayobyte', 0.011, 0.92, NULL),
  ('shifter', 0.014, 0.90, NULL),
  ('soax', 0.008, 0.96, NULL),
  ('webshare', 0.029, 0.81, NULL)
), checks(name, ord) AS (VALUES ('dns', 1), ('webrtc', 2), ('headers', 3), ('tls', 4), ('ipv6', 5))
INSERT INTO site.trust_signals(provider_slug, category, blacklist_hit_rate, stickiness_hold_rate, leak_checks, is_demo)
SELECT s.slug, 'residential', v.blacklist_hit_rate, v.stickiness_hold_rate,
       (SELECT jsonb_agg(jsonb_build_object('name', c.name, 'passed', c.name IS DISTINCT FROM v.failed_check,
                                            'note', 'TEST DATA (generated, not measured)') ORDER BY c.ord)
        FROM checks c),
       false
FROM v JOIN site.providers s ON s.framework_provider = v.provider
ON CONFLICT (provider_slug, category) DO UPDATE
SET blacklist_hit_rate = EXCLUDED.blacklist_hit_rate,
    stickiness_hold_rate = EXCLUDED.stickiness_hold_rate,
    leak_checks = EXCLUDED.leak_checks,
    is_demo = false, updated_at = now();

COMMIT;
