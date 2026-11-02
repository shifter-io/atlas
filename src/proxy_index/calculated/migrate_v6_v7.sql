-- Transactional calculated v6 → v7 migration, applied by storage.migrate() after the main
-- schema's v8 → v9 step (which creates the shared place_names and asn_names lists). Breakdown
-- places move into place_names, where they share ids with enrichment locations, and asn_labels
-- keeps its ids but names its ASN through asn_names. store.V6_CHECKS compares every breakdown's
-- resolved names with the copy taken by store.V6_SNAPSHOT. The views are replaced by schema.sql.
DROP VIEW IF EXISTS calculated_breakdown_details, current_breakdowns;
INSERT INTO place_names(country_code,country,region_code,region,city)
SELECT country_code,country,region_code,region,city FROM places ORDER BY place_id
ON CONFLICT DO NOTHING;
CREATE TEMP TABLE v7_places ON COMMIT DROP AS
SELECT p.place_id AS old,n.place_id AS new FROM places p JOIN place_names n
 ON jsonb_build_array(n.country_code,n.country,n.region_code,n.region,n.city)
  =jsonb_build_array(p.country_code,p.country,p.region_code,p.region,p.city);
DO $$
DECLARE name text;
BEGIN
 FOR name IN SELECT conname FROM pg_constraint
  WHERE conrelid='calculated_breakdowns'::regclass AND confrelid='places'::regclass LOOP
  EXECUTE format('ALTER TABLE calculated_breakdowns DROP CONSTRAINT %I', name);
 END LOOP;
END $$;
UPDATE calculated_breakdowns b SET place_id=m.new FROM v7_places m
WHERE b.place_id=m.old AND m.old<>m.new;
ALTER TABLE calculated_breakdowns ADD FOREIGN KEY(place_id) REFERENCES place_names(place_id);
DROP TABLE places;
INSERT INTO asn_names(asn,name) SELECT asn,name FROM asn_labels ORDER BY asn_label_id
ON CONFLICT DO NOTHING;
ALTER TABLE asn_labels ADD COLUMN asn_name_id integer REFERENCES asn_names(asn_name_id);
UPDATE asn_labels l SET asn_name_id=n.asn_name_id FROM asn_names n
WHERE jsonb_build_array(n.asn,n.name)=jsonb_build_array(l.asn,l.name);
ALTER TABLE asn_labels ALTER COLUMN asn_name_id SET NOT NULL;
-- Dropping asn and name also drops the old UNIQUE(asn,name,usage_type).
ALTER TABLE asn_labels DROP COLUMN asn, DROP COLUMN name;
ALTER TABLE asn_labels ADD UNIQUE NULLS NOT DISTINCT(asn_name_id,usage_type);
