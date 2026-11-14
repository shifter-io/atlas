-- Decodo residential list prices, read on https://decodo.com/proxies/residential-proxies/pricing on 2026-11-12.
-- Product: Residential rotating. Written by scripts/pricing_handoff.py (#927).
-- Run as the catalog writer role after siteapi migrate (0003 and 0005 add what it writes).
-- Only Decodo's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('decodo','Decodo','decodo')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,trial_price,trial_credit,trial_gb,trial_days,trial_needs_card,is_demo)
VALUES ('decodo','residential','per_gb',4.0,2.0,4.0,'3-day free trial (100 MB)',0.0,0.0,0.1,3,NULL,false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 trial_price=EXCLUDED.trial_price,trial_credit=EXCLUDED.trial_credit,trial_gb=EXCLUDED.trial_gb,
 trial_days=EXCLUDED.trial_days,trial_needs_card=EXCLUDED.trial_needs_card,is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='decodo' AND category='residential' AND position>=8;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('decodo','residential',0,'Regular 3 GB',11.25,3.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "poolKinds": ["residential"], "unitRate": 3.75, "originalPrice": 11.25, "originalUnitRate": 3.75, "billing": "monthly", "audience": "all", "discountSource": null, "url": "https://decodo.com/proxies/residential-proxies/pricing", "source": "https://decodo.com/proxies/residential-proxies/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('decodo','residential',1,'Regular 10 GB',35.0,10.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "poolKinds": ["residential"], "unitRate": 3.5, "originalPrice": 35.0, "originalUnitRate": 3.5, "billing": "monthly", "audience": "all", "discountSource": null, "url": "https://decodo.com/proxies/residential-proxies/pricing", "source": "https://decodo.com/proxies/residential-proxies/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('decodo','residential',2,'Regular 25 GB',81.25,25.0,NULL,NULL,true,NULL,'{"pool": "Residential rotating", "poolKinds": ["residential"], "unitRate": 3.25, "originalPrice": 81.25, "originalUnitRate": 3.25, "billing": "monthly", "audience": "all", "discountSource": null, "url": "https://decodo.com/proxies/residential-proxies/pricing", "source": "https://decodo.com/proxies/residential-proxies/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('decodo','residential',3,'Regular 50 GB',150.0,50.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "poolKinds": ["residential"], "unitRate": 3.0, "originalPrice": 150.0, "originalUnitRate": 3.0, "billing": "monthly", "audience": "all", "discountSource": null, "url": "https://decodo.com/proxies/residential-proxies/pricing", "source": "https://decodo.com/proxies/residential-proxies/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('decodo','residential',4,'Regular 100 GB',275.0,100.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "poolKinds": ["residential"], "unitRate": 2.75, "originalPrice": 275.0, "originalUnitRate": 2.75, "billing": "monthly", "audience": "all", "discountSource": null, "url": "https://decodo.com/proxies/residential-proxies/pricing", "source": "https://decodo.com/proxies/residential-proxies/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('decodo','residential',5,'Pay as you go',4.0,1.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "poolKinds": ["residential"], "unitRate": 4.0, "originalPrice": 4.0, "originalUnitRate": 4.0, "billing": "payg", "audience": "all", "discountSource": null, "url": "https://decodo.com/proxies/residential-proxies/pricing", "source": "https://decodo.com/proxies/residential-proxies/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('decodo','residential',6,'Enterprise 250 GB',625.0,250.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "poolKinds": ["residential"], "unitRate": 2.5, "originalPrice": 625.0, "originalUnitRate": 2.5, "billing": "monthly", "audience": "all", "discountSource": null, "url": "https://decodo.com/proxies/residential-proxies/pricing", "source": "https://decodo.com/proxies/residential-proxies/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('decodo','residential',7,'Enterprise 1000 GB',2000.0,1000.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "poolKinds": ["residential"], "unitRate": 2.0, "originalPrice": 2000.0, "originalUnitRate": 2.0, "billing": "monthly", "audience": "all", "discountSource": null, "url": "https://decodo.com/proxies/residential-proxies/pricing", "source": "https://decodo.com/proxies/residential-proxies/pricing", "checkedAt": "2026-11-12"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo) VALUES
('decodo','residential','2026-11-12',4.0,2.0,'https://decodo.com/proxies/residential-proxies/pricing',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
