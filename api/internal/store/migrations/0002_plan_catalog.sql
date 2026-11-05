-- Optional published pool/tier metadata; existing ungrouped plans are unchanged.
ALTER TABLE site.pricing_plans ADD COLUMN catalog jsonb
 CHECK (catalog IS NULL OR jsonb_typeof(catalog) = 'object');
