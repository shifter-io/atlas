-- Headquarters, founding year, pool claim, use cases, KYC, peer consent and
-- advertised sticky session of the 18 measured networks.
-- Read on each network's own website on 2026-11-11 (its residential page,
-- about page, terms, privacy, KYC or sourcing page; never a third party):
-- - hq: the registered or stated address, as "City, Country" (the country
--   alone where the site names no city);
-- - founded: the year the network states; null where its site states none;
-- - claim: the residential pool claim as the network writes it, and
--   claimed_pool_size that claim as a number (it feeds claimShare);
-- - best_for: up to 3 use cases the network lists, in its own words, only cut;
-- - kyc_verified: true where every buyer verifies identity before use, false
--   where buyers start without it (checks only on request or for special
--   access), null where the site says nothing;
-- - consent_disclosed: true where the site describes how peers consent (an
--   SDK, app or opt-in program), false where it gives no mechanism;
-- - stickiness_advertised: the longest sticky session, as written.
-- The page of each fact is in the PR that added this file (#862).
-- Apply after catalog-networks.sql. It writes only these columns: the
-- generated trust figures of the test days (trust-test-data.sql) in the same
-- trust row stay.
BEGIN;

WITH v(provider, hq, founded, claim, best_for) AS (VALUES
  ('byteful', 'Manchester, United Kingdom', 2020, '35M+ IPs from 195+ locations',
   ARRAY['Price Comparison', 'Brand Protection', 'Review Monitoring']),
  ('dataimpulse', 'United States', NULL, 'over 90M ethically sourced IP addresses',
   ARRAY['web scraping', 'SERP tracking', 'ad verification']),
  ('decodo', 'Vilnius, Lithuania', 2018, '115M+ Ethically-Sourced IPs in 195+ Locations',
   ARRAY['scale web scraping', 'collect diverse training data for AI and LLMs', 'verify ad placements globally']),
  ('evomi', 'Ermatingen, Switzerland', NULL, '54M+ ethically sourced IPs across 195+ countries',
   ARRAY['Search Engine Monitoring', 'AI Training', 'Localized Content Testing']),
  ('fleetproxy', 'United Arab Emirates', 2025, 'over 30 million real residential IPs across 195+ countries',
   ARRAY['Market Intelligence', 'Competitive Analysis', 'Ad Verification']),
  ('geonode', 'Singapore', NULL, '7.5M+ IPs active monthly',
   ARRAY['Web scraping', 'Price monitoring', 'Ad verification']),
  ('infatica', 'Mildenhall, United Kingdom', 2019, '40 million+ IPs',
   ARRAY['Uptime and Performance Tracking', 'Search Engine Optimization', 'Cybersecurity']),
  ('iproyal', 'Ajman, United Arab Emirates', 2020, '64M+ IPs in 195+ countries',
   ARRAY['Travel Fare Aggregation', 'Web Scraping', 'Ad Verification']),
  ('massive', 'Miami, United States', NULL, 'over 1,000,000 verified residential devices across 195+ countries',
   ARRAY['Data collection', 'Price monitoring', 'Ad verification']),
  ('nodemaven', NULL, 2023, '30M+ residential IPs',
   ARRAY['Web Scraping', 'AI Agents', 'Multi-Account Management']),
  ('oxylabs', 'Vilnius, Lithuania', 2015, '175M+ residential IPs',
   ARRAY['Market research & price monitoring', 'SEO monitoring', 'Ad verification and fraud detection']),
  ('proxy-cheap', NULL, 2018, '80 million residential IPs',
   ARRAY['Web Intelligence', 'Anonymous Browsing', 'Social Media Management']),
  ('proxy-seller', 'Larnaca, Cyprus', NULL, '47M+ Residential IPs',
   ARRAY['web scraping', 'price and assortment monitoring', 'ad analytics and creative testing']),
  ('proxyempire', 'Burgas, Bulgaria', 2020, '30M+ rotating residential IPs across 170+ countries',
   ARRAY['web data collection', 'price monitoring', 'SEO tracking']),
  ('rayobyte', 'Lincoln, United States', 2015, '50M+ opted-in devices every month',
   ARRAY['AI training and RAG', 'Web scraping', 'Price monitoring']),
  ('shifter', NULL, 2012, '205M+ Rotating IPs in 195+ Countries',
   ARRAY['Web Scraping', 'SEO Monitoring', 'Ad Verification']),
  ('soax', 'United Kingdom', 2020, 'over 12 million residential and mobile IPs',
   ARRAY['SEO Monitoring', 'Price monitoring', 'Ad verification']),
  ('webshare', 'California, United States', 2018, '80M+ ethically sourced Residential IPs',
   ARRAY['Web scraping', 'SEO', 'geo-targeting'])
)
UPDATE site.providers s
SET hq = v.hq, founded = v.founded, claim = v.claim, best_for = v.best_for, is_demo = false, updated_at = now()
FROM v
WHERE s.framework_provider = v.provider;

INSERT INTO site.trust_signals(provider_slug, category, claimed_pool_size, kyc_verified, consent_disclosed, stickiness_advertised, is_demo)
SELECT s.slug, 'residential', v.claimed_pool_size, v.kyc_verified, v.consent_disclosed, v.stickiness_advertised, false
FROM (VALUES
  ('byteful', 35000000::bigint, false, true, 'up to 24 hours'),
  ('dataimpulse', 90000000, false, false, 'up to 120 minutes'),
  ('decodo', 115000000, true, true, 'minutes to days'),
  ('evomi', 54000000, false, true, '1440 minutes — a full 24 hours'),
  ('fleetproxy', 30000000, false, false, 'up to 24 hours'),
  ('geonode', 7500000, true, true, 'up to 24 hours'),
  ('infatica', 40000000, true, true, NULL),
  ('iproyal', 64000000, false, false, 'up to 7 days'),
  ('massive', 1000000, true, true, 'up to 12 minutes'),
  ('nodemaven', 30000000, false, false, 'Up to 24 hours'),
  ('oxylabs', 175000000, true, true, 'up to 24 hours'),
  ('proxy-cheap', 80000000, NULL, false, 'for the duration of your session'),
  ('proxy-seller', 47000000, NULL, false, 'for the full duration of your task'),
  ('proxyempire', 30000000, false, false, 'no fixed time limit'),
  ('rayobyte', 50000000, false, true, 'up to 120 minutes'),
  ('shifter', 205000000, NULL, true, 'a configurable duration (ttl)'),
  ('soax', 12000000, true, false, 'a single node with no rotation'),
  ('webshare', 80000000, false, false, NULL)
) AS v(provider, claimed_pool_size, kyc_verified, consent_disclosed, stickiness_advertised)
JOIN site.providers s ON s.framework_provider = v.provider
ON CONFLICT (provider_slug, category) DO UPDATE
SET claimed_pool_size = EXCLUDED.claimed_pool_size, kyc_verified = EXCLUDED.kyc_verified,
    consent_disclosed = EXCLUDED.consent_disclosed, stickiness_advertised = EXCLUDED.stickiness_advertised,
    is_demo = false, updated_at = now();

COMMIT;
