-- Template: one site.pricing_history row per published list-rate change.
-- Run after siteapi migrate (migration 0003 creates the table). Replace the
-- placeholders with a provider's observed prices, oldest first; a row is in force
-- from effective_from until the next row of the same provider and category.
BEGIN;
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo) VALUES
('<provider-slug>','residential','<YYYY-MM-DD>',<entry $/GB>,<lowest $/GB or NULL>,'<pricing page URL> observed <YYYY-MM-DD>',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
