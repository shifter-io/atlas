-- Transactional v8 → v9 migration, applied by storage.migrate() (proxy-index db-migrate),
-- alone from v8 or after the earlier steps. Place and ASN names move out of
-- enrichment_locations and enrichment_networks into the shared place_names and asn_names lists;
-- every location_id and network_id keeps its id, so ip_enrichment and run_geo_buckets are not
-- rewritten. The v8 rows are kept in temporary tables first: migrate() checks that the new
-- views give every location, network and per-IP enrichment row unchanged (names.V8_CHECKS).
-- Definitions repeat schema.sql exactly (tests compare catalogs).
CREATE TEMP TABLE v8_before_locations ON COMMIT DROP AS SELECT * FROM enrichment_locations;
CREATE TEMP TABLE v8_before_networks ON COMMIT DROP AS SELECT * FROM enrichment_networks;
CREATE TEMP TABLE v8_before_ip_enrichment ON COMMIT DROP AS
SELECT e.ip_id,e.release_id,e.matched,l.country_code,l.country,l.region_code,l.region,l.city,
 l.latitude,l.longitude,n.isp,n.organization,n.asn,n.asn_organization,n.connection_type,n.usage_type,
 coalesce(e.extra_attributes,'{}'::jsonb) AS extra_attributes
FROM ip_enrichment e JOIN enrichment_locations l USING(location_id)
JOIN enrichment_networks n USING(network_id);
DROP VIEW IF EXISTS ip_enrichment_details;
DROP VIEW IF EXISTS ip_enrichment_fields;
CREATE TABLE place_names (
 place_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 country_code text, country text, region_code text, region text, city text,
 UNIQUE NULLS NOT DISTINCT(country_code,country,region_code,region,city)
);
CREATE INDEX place_names_city ON place_names(country_code,city);
CREATE TABLE asn_names (
 asn_name_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 asn bigint, name text, UNIQUE NULLS NOT DISTINCT(asn,name)
);
-- Names get ids in the order their first location or network was stored.
INSERT INTO place_names(country_code,country,region_code,region,city)
SELECT country_code,country,region_code,region,city FROM enrichment_locations
GROUP BY 1,2,3,4,5 ORDER BY min(location_id);
INSERT INTO asn_names(asn,name)
SELECT asn,asn_organization FROM enrichment_networks GROUP BY 1,2 ORDER BY min(network_id);
ALTER TABLE enrichment_locations ADD COLUMN place_id integer REFERENCES place_names(place_id);
ALTER TABLE enrichment_networks ADD COLUMN asn_name_id integer REFERENCES asn_names(asn_name_id);
-- NULL-safe, hashable tuple joins (as storage ingest uses) instead of a nested IS NOT DISTINCT scan.
UPDATE enrichment_locations l SET place_id=p.place_id FROM place_names p
WHERE jsonb_build_array(p.country_code,p.country,p.region_code,p.region,p.city)
 =jsonb_build_array(l.country_code,l.country,l.region_code,l.region,l.city);
UPDATE enrichment_networks n SET asn_name_id=a.asn_name_id FROM asn_names a
WHERE jsonb_build_array(a.asn,a.name)=jsonb_build_array(n.asn,n.asn_organization);
ALTER TABLE enrichment_locations ALTER COLUMN place_id SET NOT NULL;
ALTER TABLE enrichment_networks ALTER COLUMN asn_name_id SET NOT NULL;
-- Dropping the name columns also drops the v8 unique constraints and name indexes on them.
ALTER TABLE enrichment_locations DROP COLUMN country_code, DROP COLUMN country,
 DROP COLUMN region_code, DROP COLUMN region, DROP COLUMN city;
ALTER TABLE enrichment_networks DROP COLUMN asn, DROP COLUMN asn_organization;
ALTER TABLE enrichment_locations ADD UNIQUE NULLS NOT DISTINCT(place_id,latitude,longitude);
ALTER TABLE enrichment_networks
 ADD UNIQUE NULLS NOT DISTINCT(asn_name_id,isp,organization,connection_type,usage_type);
