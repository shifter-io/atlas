-- Rayobyte residential list prices, read on https://rayobyte.com/pricing/ on 2026-11-12.
-- Product: Premium residential. Written by scripts/pricing_handoff.py (#927).
-- Run as the catalog writer role after siteapi migrate (0003 and 0005 add what it writes).
-- Only Rayobyte's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('rayobyte','Rayobyte','rayobyte')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,trial_price,trial_credit,trial_gb,trial_days,trial_needs_card,is_demo)
VALUES ('rayobyte','residential','per_gb',3.5,0.7,3.5,'Free trial (no card needed)',0.0,0.0,NULL,NULL,false,false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 trial_price=EXCLUDED.trial_price,trial_credit=EXCLUDED.trial_credit,trial_gb=EXCLUDED.trial_gb,
 trial_days=EXCLUDED.trial_days,trial_needs_card=EXCLUDED.trial_needs_card,is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='rayobyte' AND category='residential' AND position>=4;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('rayobyte','residential',0,'PAYG 1 to 49 GB',3.5,1.0,NULL,NULL,false,NULL,'{"pool": "Premium residential", "poolKinds": ["residential"], "unitRate": 3.5, "originalPrice": 3.5, "originalUnitRate": 3.5, "billing": "payg", "audience": "all", "discountSource": "volume", "url": "https://rayobyte.com/pricing/", "source": "https://rayobyte.com/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('rayobyte','residential',1,'PAYG 50 to 249 GB',100.0,50.0,NULL,NULL,false,42.9,'{"pool": "Premium residential", "poolKinds": ["residential"], "unitRate": 2.0, "originalPrice": 175.0, "originalUnitRate": 3.5, "billing": "payg", "audience": "all", "discountSource": "volume", "url": "https://rayobyte.com/pricing/", "source": "https://rayobyte.com/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('rayobyte','residential',2,'PAYG 250 to 999 GB',375.0,250.0,NULL,NULL,false,57.1,'{"pool": "Premium residential", "poolKinds": ["residential"], "unitRate": 1.5, "originalPrice": 875.0, "originalUnitRate": 3.5, "billing": "payg", "audience": "all", "discountSource": "volume", "url": "https://rayobyte.com/pricing/", "source": "https://rayobyte.com/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('rayobyte','residential',3,'PAYG 1,000 to 4,999 GB',700.0,1000.0,NULL,NULL,false,80.0,'{"pool": "Premium residential", "poolKinds": ["residential"], "unitRate": 0.7, "originalPrice": 3500.0, "originalUnitRate": 3.5, "billing": "payg", "audience": "all", "discountSource": "volume", "url": "https://rayobyte.com/pricing/", "source": "https://rayobyte.com/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo) VALUES
('rayobyte','residential','2026-11-12',3.5,0.7,'https://rayobyte.com/pricing/',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
