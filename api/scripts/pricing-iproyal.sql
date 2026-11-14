-- IPRoyal residential list prices, read on https://iproyal.com/pricing/residential-proxies/ on 2026-11-12.
-- Product: Residential rotating. Written by scripts/pricing_handoff.py (#927).
-- Run as the catalog writer role after siteapi migrate (0003 and 0005 add what it writes).
-- Only IPRoyal's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('iproyal','IPRoyal','iproyal')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,trial_price,trial_credit,trial_gb,trial_days,trial_needs_card,is_demo)
VALUES ('iproyal','residential','per_gb',7.35,5.25,7.35,'None',NULL,NULL,NULL,NULL,NULL,false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 trial_price=EXCLUDED.trial_price,trial_credit=EXCLUDED.trial_credit,trial_gb=EXCLUDED.trial_gb,
 trial_days=EXCLUDED.trial_days,trial_needs_card=EXCLUDED.trial_needs_card,is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='iproyal' AND category='residential' AND position>=6;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('iproyal','residential',0,NULL,7.0,1.0,NULL,NULL,false,4.8,'{"pool": null, "poolKinds": ["residential"], "unitRate": 7.0, "originalPrice": 7.35, "originalUnitRate": 7.35, "billing": "monthly", "audience": "all", "discountSource": "subscription", "url": "https://iproyal.com/pricing/residential-proxies/", "source": "https://iproyal.com/pricing/residential-proxies/", "checkedAt": "2026-11-12"}'::jsonb,false),
('iproyal','residential',1,NULL,7.35,1.0,NULL,NULL,false,NULL,'{"pool": null, "poolKinds": ["residential"], "unitRate": 7.35, "originalPrice": 7.35, "originalUnitRate": 7.35, "billing": "payg", "audience": "all", "discountSource": null, "url": "https://iproyal.com/pricing/residential-proxies/", "source": "https://iproyal.com/pricing/residential-proxies/", "checkedAt": "2026-11-12"}'::jsonb,false),
('iproyal','residential',2,NULL,11.9,2.0,NULL,NULL,false,4.8,'{"pool": null, "poolKinds": ["residential"], "unitRate": 5.95, "originalPrice": 12.5, "originalUnitRate": 6.25, "billing": "monthly", "audience": "all", "discountSource": "subscription", "url": "https://iproyal.com/pricing/residential-proxies/", "source": "https://iproyal.com/pricing/residential-proxies/", "checkedAt": "2026-11-12"}'::jsonb,false),
('iproyal','residential',3,NULL,12.5,2.0,NULL,NULL,false,NULL,'{"pool": null, "poolKinds": ["residential"], "unitRate": 6.25, "originalPrice": 12.5, "originalUnitRate": 6.25, "billing": "payg", "audience": "all", "discountSource": null, "url": "https://iproyal.com/pricing/residential-proxies/", "source": "https://iproyal.com/pricing/residential-proxies/", "checkedAt": "2026-11-12"}'::jsonb,false),
('iproyal','residential',4,NULL,52.5,10.0,NULL,NULL,false,4.7,'{"pool": null, "poolKinds": ["residential"], "unitRate": 5.25, "originalPrice": 55.1, "originalUnitRate": 5.51, "billing": "monthly", "audience": "all", "discountSource": "subscription", "url": "https://iproyal.com/pricing/residential-proxies/", "source": "https://iproyal.com/pricing/residential-proxies/", "checkedAt": "2026-11-12"}'::jsonb,false),
('iproyal','residential',5,NULL,55.1,10.0,NULL,NULL,false,NULL,'{"pool": null, "poolKinds": ["residential"], "unitRate": 5.51, "originalPrice": 55.1, "originalUnitRate": 5.51, "billing": "payg", "audience": "all", "discountSource": null, "url": "https://iproyal.com/pricing/residential-proxies/", "source": "https://iproyal.com/pricing/residential-proxies/", "checkedAt": "2026-11-12"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo) VALUES
('iproyal','residential','2026-11-12',7.35,5.25,'https://iproyal.com/pricing/residential-proxies/',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
