-- Published list prices over time, one row per change: the rate a product category's catalog
-- listed from effective_from until the next row. The API's price history (/v1/networks/{slug}/
-- pricing-history) reads the rate in force each day; source is where the price was observed.
CREATE TABLE site.pricing_history (
 provider_slug text NOT NULL REFERENCES site.providers(slug) ON DELETE CASCADE,
 category text NOT NULL CHECK (category IN ('residential','datacenter','mobile','isp')),
 effective_from date NOT NULL,
 price_per_gb double precision, price_per_gb_at_scale double precision,
 source text,
 is_demo boolean NOT NULL DEFAULT false,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (provider_slug, category, effective_from),
 CHECK (price_per_gb IS NOT NULL OR price_per_gb_at_scale IS NOT NULL)
);

CREATE TRIGGER pricing_history_revision AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON site.pricing_history
 FOR EACH STATEMENT EXECUTE FUNCTION site.bump_revision();
CREATE TRIGGER pricing_history_touch BEFORE UPDATE ON site.pricing_history
 FOR EACH ROW EXECUTE FUNCTION site.touch_updated_at();
