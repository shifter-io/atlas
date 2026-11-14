-- SOAX residential list prices, read on https://soax.com/pricing on 2026-11-12.
-- Product: Residential + mobile. Written by scripts/pricing_handoff.py (#927).
-- Run as the catalog writer role after siteapi migrate (0003 and 0005 add what it writes).
-- Only SOAX's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('soax','SOAX','soax')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,trial_price,trial_credit,trial_gb,trial_days,trial_needs_card,is_demo)
VALUES ('soax','residential','per_gb',5.0,0.85,5.0,'None',NULL,NULL,NULL,NULL,NULL,false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 trial_price=EXCLUDED.trial_price,trial_credit=EXCLUDED.trial_credit,trial_gb=EXCLUDED.trial_gb,
 trial_days=EXCLUDED.trial_days,trial_needs_card=EXCLUDED.trial_needs_card,is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='soax' AND category='residential' AND position>=5;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('soax','residential',0,'Sandbox, geo Tier 1',5.0,NULL,NULL,NULL,false,NULL,'{"pool": "Residential + mobile", "poolKinds": ["residential", "mobile"], "unitRate": 5.0, "originalPrice": 5.0, "originalUnitRate": 5.0, "billing": "payg", "audience": "all", "discountSource": null, "url": "https://soax.com/pricing", "source": "https://soax.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('soax','residential',1,'Builder, geo Tier 1',200.0,66.7,NULL,NULL,false,NULL,'{"pool": "Residential + mobile", "poolKinds": ["residential", "mobile"], "unitRate": 3.0, "originalPrice": 200.0, "originalUnitRate": 3.0, "billing": "monthly", "audience": "all", "discountSource": null, "url": "https://soax.com/pricing", "source": "https://soax.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('soax','residential',2,'Team, geo Tier 1',500.0,227.3,NULL,NULL,false,NULL,'{"pool": "Residential + mobile", "poolKinds": ["residential", "mobile"], "unitRate": 2.2, "originalPrice": 500.0, "originalUnitRate": 2.2, "billing": "monthly", "audience": "all", "discountSource": null, "url": "https://soax.com/pricing", "source": "https://soax.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('soax','residential',3,'Scale, geo Tier 1',1500.0,1000.0,NULL,NULL,false,NULL,'{"pool": "Residential + mobile", "poolKinds": ["residential", "mobile"], "unitRate": 1.5, "originalPrice": 1500.0, "originalUnitRate": 1.5, "billing": "monthly", "audience": "all", "discountSource": null, "url": "https://soax.com/pricing", "source": "https://soax.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('soax','residential',4,'Enterprise, geo Tier 1',3000.0,3529.4,NULL,NULL,false,NULL,'{"pool": "Residential + mobile", "poolKinds": ["residential", "mobile"], "unitRate": 0.85, "originalPrice": 3000.0, "originalUnitRate": 0.85, "billing": "monthly", "audience": "all", "discountSource": null, "url": "https://soax.com/pricing", "source": "https://soax.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo) VALUES
('soax','residential','2026-11-12',5.0,0.85,'https://soax.com/pricing',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
