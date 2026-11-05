-- Restore the provider link used by the shared review actions.
-- Apply to the site's database after its catalog schema exists.
-- Source: https://shifter.io/services/residential-proxies
-- Existing catalog copy and custom links remain intact.
BEGIN;

INSERT INTO site.providers(slug, name, framework_provider)
SELECT 'shifter', 'Shifter', id FROM public.providers
WHERE id = 'shifter'
  AND NOT EXISTS (SELECT 1 FROM site.providers WHERE framework_provider = 'shifter')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO site.provider_products(provider_slug, product_kind, name, measured, url, framework_product)
SELECT slug, 'residential_rotating', 'Residential', true,
       'https://shifter.io/services/residential-proxies', 'residential'
FROM site.providers
WHERE framework_provider = 'shifter'
ON CONFLICT (provider_slug, product_kind) DO UPDATE
SET url = coalesce(nullif(trim(site.provider_products.url), ''), EXCLUDED.url),
    updated_at = now();

COMMIT;
