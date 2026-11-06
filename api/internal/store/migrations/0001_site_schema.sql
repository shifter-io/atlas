-- Site schema v1: catalog and editorial data the framework does not measure, owned by the site API
-- (github.com/Zeus-Development/proxy-index/api). The framework's public schema is only ever read.
--
-- Every row carries is_demo. The four design fixtures (Nimbus, Kestrel, Orbitnode, Helix) are
-- is_demo = true and `siteapi unseed-demo` deletes exactly those rows. Real providers are linked to
-- the framework through providers.framework_provider (= public.providers.id) and
-- provider_products.framework_product (= public.products.name). There are no foreign keys into the
-- framework schema, so neither side can block the other's migrations.
--
-- Typed columns hold whatever the site sorts, filters or joins on; deeply nested, demo-only
-- measurement documents (series, IP samples, incidents, geo, ASNs, overlap) are JSONB.
CREATE SCHEMA IF NOT EXISTS site;

-- Bumped by a statement trigger on every write to any site table, so the API's change detection
-- also sees deletes. One row.
CREATE TABLE site.revision (
 singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
 value bigint NOT NULL DEFAULT 0,
 changed_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO site.revision DEFAULT VALUES;

CREATE FUNCTION site.bump_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 UPDATE site.revision SET value = value + 1, changed_at = clock_timestamp();
 RETURN NULL;
END $$;

CREATE FUNCTION site.touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 NEW.updated_at := clock_timestamp();
 RETURN NEW;
END $$;

-- Dataset-level metadata of a loaded fixture (the demo's generatedAt anchors its relative ages).
CREATE TABLE site.datasets (
 name text PRIMARY KEY,
 schema_version text NOT NULL,
 generated_at timestamptz NOT NULL,
 seed bigint,
 run_id text,
 collector_version text,
 window_hours integer,
 source_file text,
 source_sha256 text,
 is_demo boolean NOT NULL DEFAULT false,
 updated_at timestamptz NOT NULL DEFAULT now()
);

-- Target presentation. Framework targets (public.targets) are listed even without a row here.
CREATE TABLE site.targets (
 target_id text PRIMARY KEY,
 label text,
 category text,
 pass_condition text,
 difficulty smallint CHECK (difficulty BETWEEN 1 AND 5),
 sort_order integer NOT NULL DEFAULT 0,
 is_demo boolean NOT NULL DEFAULT false,
 updated_at timestamptz NOT NULL DEFAULT now()
);

-- What gets tested for each product type (methodology copy).
CREATE TABLE site.scopes (
 product_kind text PRIMARY KEY
  CHECK (product_kind IN ('residential_rotating','residential_sticky','isp_static','datacenter','mobile')),
 label text, summary text, cadence text,
 all_targets boolean NOT NULL DEFAULT true,
 target_ids text[] NOT NULL DEFAULT '{}',
 live_ip_sampling boolean, pool_tracking boolean, overlap_audit boolean, stickiness_test boolean,
 geo_coverage text, rationale text,
 sort_order integer NOT NULL DEFAULT 0,
 is_demo boolean NOT NULL DEFAULT false,
 updated_at timestamptz NOT NULL DEFAULT now()
);

-- Default composite weights, used when no published snapshot carries methodology weights.
CREATE TABLE site.score_weights (
 component text PRIMARY KEY
  CHECK (component IN ('reliability','speed','uptime','coverage','diversity','dcAsns','authenticity','value')),
 weight double precision NOT NULL CHECK (weight >= 0),
 is_demo boolean NOT NULL DEFAULT false,
 updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE site.ranking_profiles (
 profile_id text PRIMARY KEY,
 label text, description text,
 weights jsonb NOT NULL CHECK (jsonb_typeof(weights) = 'object'),
 sort_order integer NOT NULL DEFAULT 0,
 is_demo boolean NOT NULL DEFAULT false,
 updated_at timestamptz NOT NULL DEFAULT now()
);

-- Provider presentation. Score columns are only used by rows without a framework measurement
-- (demo fixtures); a measured provider's score always comes from the published snapshot.
CREATE TABLE site.providers (
 slug text PRIMARY KEY CHECK (slug ~ '^[a-z0-9][a-z0-9-]*$'),
 name text NOT NULL,
 tagline text,
 kinds text[] NOT NULL DEFAULT '{}'
  CHECK (kinds <@ ARRAY['residential','datacenter','mobile','isp']),
 primary_kind text CHECK (primary_kind IN ('residential','datacenter','mobile','isp')),
 measured_product text
  CHECK (measured_product IN ('residential_rotating','residential_sticky','isp_static','datacenter','mobile')),
 hq text, founded integer, claim text, verdict text,
 strengths text[], weaknesses text[], best_for text[],
 website text,
 framework_provider text UNIQUE,
 listed boolean NOT NULL DEFAULT true,
 sort_order integer NOT NULL DEFAULT 0,
 score_composite double precision, score_rank integer, score_previous_rank integer,
 score_grade text, score_breakdown jsonb,
 -- The headline overlap block when it differs from the primary product's (demo fixtures only).
 headline_overlap jsonb,
 is_demo boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK (NOT is_demo OR framework_provider IS NULL)
);
CREATE INDEX providers_demo ON site.providers (is_demo);

CREATE TABLE site.provider_products (
 provider_slug text NOT NULL REFERENCES site.providers(slug) ON DELETE CASCADE,
 product_kind text NOT NULL
  CHECK (product_kind IN ('residential_rotating','residential_sticky','isp_static','datacenter','mobile')),
 name text, measured boolean NOT NULL DEFAULT false, url text, price_label text, note text,
 framework_product text,
 sort_order integer NOT NULL DEFAULT 0,
 is_demo boolean NOT NULL DEFAULT false,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (provider_slug, product_kind)
);

-- Published list pricing per product category ($ / GB is the site's compact price metric).
CREATE TABLE site.pricing (
 provider_slug text NOT NULL REFERENCES site.providers(slug) ON DELETE CASCADE,
 category text NOT NULL CHECK (category IN ('residential','datacenter','mobile','isp')),
 model text CHECK (model IN ('per_gb','per_ip','per_port','per_request')),
 price_per_gb double precision, price_per_gb_at_scale double precision,
 min_commit double precision, trial text,
 cost_per_1k_success double precision, avg_payload_kb double precision, cost_basis text,
 headline_price_rank integer, value_rank integer,
 is_demo boolean NOT NULL DEFAULT false,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (provider_slug, category)
);
CREATE INDEX pricing_per_gb ON site.pricing (category, price_per_gb);

CREATE TABLE site.pricing_plans (
 provider_slug text NOT NULL,
 category text NOT NULL,
 position integer NOT NULL,
 name text, price double precision, gb double precision, ips double precision, note text,
 recommended boolean, discount_pct double precision,
 is_demo boolean NOT NULL DEFAULT false,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (provider_slug, category, position),
 FOREIGN KEY (provider_slug, category) REFERENCES site.pricing(provider_slug, category) ON DELETE CASCADE
);

-- Trust signals per category. Measured fields (DC ASN share) from the snapshot take precedence.
CREATE TABLE site.trust_signals (
 provider_slug text NOT NULL REFERENCES site.providers(slug) ON DELETE CASCADE,
 category text NOT NULL CHECK (category IN ('residential','datacenter','mobile','isp')),
 residential_authenticity double precision, datacenter_asn_share double precision,
 claim_gap double precision, blacklist_hit_rate double precision,
 stickiness_hold_rate double precision, stickiness_advertised text,
 -- The provider's own marketing claim for the pool, compared against measured unique IPs.
 claimed_pool_size bigint,
 leak_checks jsonb CHECK (leak_checks IS NULL OR jsonb_typeof(leak_checks) = 'array'),
 kyc_verified boolean, consent_disclosed boolean,
 is_demo boolean NOT NULL DEFAULT false,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (provider_slug, category)
);

-- Measurement documents of providers that have no framework measurement (the demo fixtures).
CREATE TABLE site.measurements (
 provider_slug text NOT NULL REFERENCES site.providers(slug) ON DELETE CASCADE,
 category text NOT NULL CHECK (category IN ('residential','datacenter','mobile','isp')),
 product_kind text NOT NULL
  CHECK (product_kind IN ('residential_rotating','residential_sticky','isp_static','datacenter','mobile')),
 position integer NOT NULL DEFAULT 0,
 pool jsonb NOT NULL, performance jsonb NOT NULL, targets jsonb NOT NULL, series jsonb NOT NULL,
 ip_samples jsonb NOT NULL, incidents jsonb NOT NULL, geo jsonb NOT NULL, asns jsonb NOT NULL,
 overlap jsonb NOT NULL,
 -- Typed copies of the headline numbers, for sorting and ad-hoc queries.
 success_rate double precision, latency_p50 double precision, unique_ips_24h double precision,
 is_demo boolean NOT NULL DEFAULT false,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (provider_slug, category)
);

CREATE TABLE site.reviews (
 review_id text PRIMARY KEY,
 provider_slug text NOT NULL,
 author text NOT NULL, role text NOT NULL,
 rating smallint NOT NULL CHECK (rating BETWEEN 1 AND 5),
 title text NOT NULL, body text NOT NULL,
 product_kind text NOT NULL, use_case text NOT NULL, monthly_spend text NOT NULL,
 months_used integer NOT NULL,
 verification text NOT NULL CHECK (verification IN ('invoice_verified','run_verified','unverified','pending')),
 invoice_month text, linked_run_id text, helpful_count integer NOT NULL DEFAULT 0,
 provider_reply text,
 posted_at timestamptz NOT NULL,
 position integer,
 is_demo boolean NOT NULL DEFAULT false,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX reviews_provider ON site.reviews (provider_slug, posted_at DESC);

CREATE TABLE site.community_runs (
 run_id text PRIMARY KEY,
 provider_slug text NOT NULL,
 submitted_by text NOT NULL, region text NOT NULL, product_kind text NOT NULL,
 plan_tier text NOT NULL, collector_version text NOT NULL,
 status text NOT NULL CHECK (status IN ('accepted','rejected','pending')),
 rejection_reason text,
 requests bigint, success_rate double precision, latency_p50 double precision,
 unique_ips bigint, overlap_share double precision,
 delta_success_pts double precision, delta_latency_pct double precision,
 targets jsonb NOT NULL DEFAULT '[]', success_series jsonb NOT NULL DEFAULT '[]',
 latency_series jsonb NOT NULL DEFAULT '[]',
 submitted_at timestamptz NOT NULL,
 position integer,
 is_demo boolean NOT NULL DEFAULT false,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX community_runs_provider ON site.community_runs (provider_slug, submitted_at DESC);

CREATE TABLE site.changelog (
 change_id text PRIMARY KEY,
 kind text NOT NULL
  CHECK (kind IN ('pool','pricing','performance','incident','authenticity','coverage','methodology')),
 provider_slug text,
 headline text NOT NULL, detail text NOT NULL,
 delta double precision, unit text NOT NULL DEFAULT '',
 severity text NOT NULL CHECK (severity IN ('info','good','bad')),
 occurred_at timestamptz NOT NULL,
 position integer,
 is_demo boolean NOT NULL DEFAULT false,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX changelog_time ON site.changelog (occurred_at DESC);

-- Editorial tape rows. Measured tape rows come from public.test_events and are never copied here.
CREATE TABLE site.tape_events (
 tape_id text PRIMARY KEY,
 kind text NOT NULL,
 provider_slug text NOT NULL,
 product text NOT NULL CHECK (product IN ('residential','datacenter','mobile','isp')),
 title text NOT NULL, detail text NOT NULL,
 signal smallint NOT NULL DEFAULT 0, value text NOT NULL DEFAULT '',
 occurred_at timestamptz NOT NULL,
 position integer,
 is_demo boolean NOT NULL DEFAULT false,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tape_events_time ON site.tape_events (occurred_at DESC);

DO $$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY['datasets','targets','scopes','score_weights','ranking_profiles','providers',
  'provider_products','pricing','pricing_plans','trust_signals','measurements','reviews',
  'community_runs','changelog','tape_events'] LOOP
  EXECUTE format('CREATE TRIGGER %I AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON site.%I '
   'FOR EACH STATEMENT EXECUTE FUNCTION site.bump_revision()', t || '_revision', t);
  EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE ON site.%I '
   'FOR EACH ROW EXECUTE FUNCTION site.touch_updated_at()', t || '_touch', t);
 END LOOP;
END $$;
