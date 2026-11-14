-- DataImpulse residential list prices, read on https://dataimpulse.com/residential-proxies/ on 2026-11-12.
-- Product: Residential rotating. Written by scripts/pricing_handoff.py (#927).
-- Run as the catalog writer role after siteapi migrate (0003 and 0005 add what it writes).
-- Only DataImpulse's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('dataimpulse','DataImpulse','dataimpulse')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,trial_price,trial_credit,trial_gb,trial_days,trial_needs_card,is_demo)
VALUES ('dataimpulse','residential','per_gb',1.0,0.8,5.0,'None',NULL,NULL,NULL,NULL,NULL,false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 trial_price=EXCLUDED.trial_price,trial_credit=EXCLUDED.trial_credit,trial_gb=EXCLUDED.trial_gb,
 trial_days=EXCLUDED.trial_days,trial_needs_card=EXCLUDED.trial_needs_card,is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='dataimpulse' AND category='residential' AND position>=3;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('dataimpulse','residential',0,'Intro',5.0,5.0,NULL,NULL,false,NULL,'{"pool": null, "poolKinds": ["residential"], "unitRate": 1.0, "originalPrice": 5.0, "originalUnitRate": 1.0, "billing": "payg", "audience": "new_customers", "discountSource": null, "url": "https://dataimpulse.com/residential-proxies/", "source": "https://dataimpulse.com/residential-proxies/", "checkedAt": "2026-11-12"}'::jsonb,false),
('dataimpulse','residential',1,'Basic',50.0,50.0,NULL,NULL,false,NULL,'{"pool": null, "poolKinds": ["residential"], "unitRate": 1.0, "originalPrice": 50.0, "originalUnitRate": 1.0, "billing": "payg", "audience": "all", "discountSource": null, "url": "https://dataimpulse.com/residential-proxies/", "source": "https://dataimpulse.com/residential-proxies/", "checkedAt": "2026-11-12"}'::jsonb,false),
('dataimpulse','residential',2,'Advanced',800.0,1000.0,NULL,NULL,true,NULL,'{"pool": null, "poolKinds": ["residential"], "unitRate": 0.8, "originalPrice": 800.0, "originalUnitRate": 0.8, "billing": "payg", "audience": "all", "discountSource": "volume", "url": "https://dataimpulse.com/residential-proxies/", "source": "https://dataimpulse.com/residential-proxies/", "checkedAt": "2026-11-12"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo) VALUES
('dataimpulse','residential','2026-11-12',1.0,0.8,'https://dataimpulse.com/residential-proxies/',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
