-- Language-neutral provider facts, reviews and community runs (PROVIDER-CODES-1): a network's
-- headquarters as an ISO 3166-1 country code and a city name (null where the catalog names no
-- city), its use cases as codes of the API's use_case set (/v1/codes), a review's language
-- (BCP 47) and monthly spend bounds in US dollars, and a run's country (ISO code). Additions
-- only: the English hq, best_for, monthly_spend and region stay until the website reads these.
ALTER TABLE site.providers
 ADD COLUMN hq_country text CHECK (hq_country ~ '^[A-Z]{2}$'),
 ADD COLUMN hq_city text CHECK (btrim(hq_city) <> ''),
 ADD COLUMN use_cases text[] CHECK (use_cases <@ ARRAY['web_scraping','price_monitoring','ad_verification',
  'seo_monitoring','serp_tracking','brand_protection','review_monitoring','market_research','ai_training_data',
  'ai_agents','travel_fares','social_media','multi_accounting','cybersecurity','localized_testing',
  'uptime_monitoring','anonymous_browsing']),
 ADD CHECK (hq_city IS NULL OR hq_country IS NOT NULL);

ALTER TABLE site.reviews
 ADD COLUMN lang text CHECK (lang ~ '^[a-z]{2,3}(-[A-Za-z0-9]{1,8})*$'),
 ADD COLUMN monthly_spend_min double precision CHECK (monthly_spend_min >= 0),
 ADD COLUMN monthly_spend_max double precision CHECK (monthly_spend_max >= 0),
 ADD CHECK (monthly_spend_min <= monthly_spend_max);

ALTER TABLE site.community_runs
 ADD COLUMN country text CHECK (country ~ '^[A-Z]{2}$');
