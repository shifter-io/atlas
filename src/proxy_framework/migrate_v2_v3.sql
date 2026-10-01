-- Transactional v2 → v3 migration. Preserve unknown fields without duplicating known fields.
DROP VIEW ip_enrichment_details;
ALTER TABLE ip_enrichment ADD COLUMN matched boolean;
ALTER TABLE ip_enrichment ADD COLUMN country text;
ALTER TABLE ip_enrichment ADD COLUMN latitude double precision;
ALTER TABLE ip_enrichment ADD COLUMN longitude double precision;
ALTER TABLE ip_enrichment ADD COLUMN organization text;
ALTER TABLE ip_enrichment ADD COLUMN asn_organization text;
ALTER TABLE ip_enrichment ADD COLUMN connection_type text;
ALTER TABLE ip_enrichment ADD COLUMN usage_type text;
ALTER TABLE ip_enrichment ADD COLUMN extra_attributes jsonb NOT NULL DEFAULT '{}';
UPDATE ip_enrichment SET
 matched=(attributes->>'matched')::boolean,
 country_code=(attributes->>'country_code')::text,
 country=(attributes->>'country')::text,
 region_code=(attributes->>'region_code')::text,
 region=(attributes->>'region')::text,
 city=(attributes->>'city')::text,
 latitude=(attributes->>'latitude')::double precision,
 longitude=(attributes->>'longitude')::double precision,
 isp=(attributes->>'isp')::text,
 organization=(attributes->>'organization')::text,
 asn=(attributes->>'asn')::bigint,
 asn_organization=(attributes->>'asn_organization')::text,
 connection_type=(attributes->>'connection_type')::text,
 usage_type=(attributes->>'usage_type')::text,
 extra_attributes=attributes - ARRAY['matched','country_code','country','region_code','region','city','latitude','longitude','isp','organization','asn','asn_organization','connection_type','usage_type']::text[];
ALTER TABLE ip_enrichment DROP COLUMN attributes;
CREATE OR REPLACE VIEW ip_enrichment_details AS
SELECT a.ip,r.sha256 AS dbip_release,
       e.matched,
       e.country_code,
       e.country,
       e.region_code,
       e.region,
       e.city,
       e.latitude,
       e.longitude,
       e.isp,
       e.organization,
       e.asn,
       e.asn_organization,
       e.connection_type,
       e.usage_type,
       e.extra_attributes,e.ip_id,e.release_id
FROM ip_enrichment e JOIN ip_addresses a USING(ip_id) JOIN dbip_releases r USING(release_id);
