-- Name, tagline and website of the 18 measured networks, and their measured
-- residential product, linked to the framework provider and product.
-- Read on each brand's own website on 2026-11-11: the name as the brand writes
-- itself (logo text and page title), the tagline quoted from its residential
-- page (meta description or headline, at most 90 characters).
-- Apply to the site's database after its catalog schema exists. It runs before
-- or after the price scripts, which may have inserted a minimal provider row;
-- a row already linked to the framework provider keeps its slug, and an
-- existing product link stays.
BEGIN;

WITH v(provider, name, tagline, website) AS (VALUES
  ('byteful', 'Byteful', 'Get fast residential proxies with 35M+ IPs from 195+ locations.', 'https://byteful.com'),
  ('dataimpulse', 'DataImpulse', 'Residential Proxies –$1 per GB, Traffic Never Expires', 'https://dataimpulse.com'),
  ('decodo', 'Decodo', 'Get premium residential proxies with 115M+ IPs from 195+ locations.', 'https://decodo.com'),
  ('evomi', 'Evomi', 'Buy ethically sourced residential proxies from $0.49/GB.', 'https://evomi.com'),
  ('fleetproxy', 'FleetProxy', 'Use 30M+ residential IPs across 195 countries.', 'https://fleetproxy.io'),
  ('geonode', 'Geonode', 'Residential proxies from $0.27/GB', 'https://geonode.com'),
  ('infatica', 'Infatica', '40M+ residential proxies with country, region and city targeting!', 'https://infatica.io'),
  ('iproyal', 'IPRoyal', 'Get access to over 64M+ ethically-sourced residential proxies in more than 195 locations.', 'https://iproyal.com'),
  ('massive', 'Massive', 'Route requests through real consumer devices in 195+ countries.', 'https://www.joinmassive.com'),
  ('nodemaven', 'NodeMaven', 'Buy clean residential proxies with 30M+ clean IPs in 190+ countries.', 'https://nodemaven.com'),
  ('oxylabs', 'Oxylabs', 'Residential Proxies with 175M+ IPs in 195 countries.', 'https://oxylabs.io'),
  ('proxy-cheap', 'Proxy-Cheap', 'Buy fast, affordable rotating residential proxies from Proxy-Cheap.', 'https://www.proxy-cheap.com'),
  ('proxy-seller', 'Proxy-Seller', 'Buy residential proxy from $1.45/GB.', 'https://proxy-seller.com'),
  ('proxyempire', 'ProxyEmpire', 'Rotating residential proxies: 30M+ real home IPs in 170+ countries.', 'https://proxyempire.io'),
  ('rayobyte', 'Rayobyte', 'Premium Residential Proxies from 50M+ opted-in devices every month.', 'https://rayobyte.com'),
  ('shifter', 'Shifter', 'Buy rotating residential proxies from $0.10/GB.', 'https://shifter.io'),
  ('soax', 'SOAX', 'Residential proxies for teams who need web data at scale', 'https://soax.com'),
  ('webshare', 'Webshare', 'Rotating residential proxies from $1.40/GB.', 'https://www.webshare.io')
), linked AS (
  UPDATE site.providers s
  SET name = v.name, tagline = v.tagline, website = v.website,
      kinds = CASE WHEN s.kinds = '{}' THEN '{residential}' ELSE s.kinds END,
      primary_kind = coalesce(s.primary_kind, 'residential'),
      measured_product = coalesce(s.measured_product, 'residential_rotating'),
      updated_at = now()
  FROM v
  WHERE s.framework_provider = v.provider
  RETURNING s.framework_provider
)
INSERT INTO site.providers(slug, name, tagline, website, kinds, primary_kind, measured_product, framework_provider, is_demo)
SELECT v.provider, v.name, v.tagline, v.website, '{residential}', 'residential', 'residential_rotating', p.id, false
FROM v JOIN public.providers p ON p.id = v.provider
WHERE v.provider NOT IN (SELECT framework_provider FROM linked)
ON CONFLICT (slug) DO UPDATE
SET name = EXCLUDED.name, tagline = EXCLUDED.tagline, website = EXCLUDED.website,
    kinds = CASE WHEN site.providers.kinds = '{}' THEN EXCLUDED.kinds ELSE site.providers.kinds END,
    primary_kind = coalesce(site.providers.primary_kind, EXCLUDED.primary_kind),
    measured_product = coalesce(site.providers.measured_product, EXCLUDED.measured_product),
    framework_provider = coalesce(site.providers.framework_provider, EXCLUDED.framework_provider),
    is_demo = false, updated_at = now();

INSERT INTO site.provider_products(provider_slug, product_kind, name, measured, url, framework_product, is_demo)
SELECT s.slug, 'residential_rotating', 'Residential', true, v.url, 'residential', false
FROM (VALUES
  ('byteful', 'https://byteful.com/proxy-service/residential-proxies'),
  ('dataimpulse', 'https://dataimpulse.com/residential-proxies/'),
  ('decodo', 'https://decodo.com/proxies/residential-proxies'),
  ('evomi', 'https://evomi.com/product/residential-proxies'),
  ('fleetproxy', 'https://fleetproxy.io/proxies/rotating-residential'),
  ('geonode', 'https://geonode.com/products/residential-proxies'),
  ('infatica', 'https://infatica.io/residential-proxies/'),
  ('iproyal', 'https://iproyal.com/residential-proxies/'),
  ('massive', 'https://www.joinmassive.com/web-access'),
  ('nodemaven', 'https://nodemaven.com/proxies/residential-proxies/'),
  ('oxylabs', 'https://oxylabs.io/products/residential-proxy-pool'),
  ('proxy-cheap', 'https://www.proxy-cheap.com/services/rotating-residential-proxies'),
  ('proxy-seller', 'https://proxy-seller.com/residential-proxies/'),
  ('proxyempire', 'https://proxyempire.io/rotating-residential-proxies/'),
  ('rayobyte', 'https://rayobyte.com/products/residential-proxies/'),
  ('shifter', 'https://shifter.io/services/residential-proxies'),
  ('soax', 'https://soax.com/proxies/residential'),
  ('webshare', 'https://www.webshare.io/residential-proxy')
) AS v(provider, url)
JOIN site.providers s ON s.framework_provider = v.provider
ON CONFLICT (provider_slug, product_kind) DO UPDATE
SET measured = true,
    url = coalesce(nullif(trim(site.provider_products.url), ''), EXCLUDED.url),
    framework_product = coalesce(site.provider_products.framework_product, EXCLUDED.framework_product),
    is_demo = false, updated_at = now();

COMMIT;
