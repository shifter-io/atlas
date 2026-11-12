-- Massive residential list prices, read on https://www.joinmassive.com/web-access on 2026-11-12.
-- Product: Residential rotating. Written by scripts/pricing_handoff.py (#927).
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only Massive's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('massive','Massive','massive')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('massive','residential','per_gb',8.0,4.9,80.0,'None',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='massive' AND category='residential' AND position>=4;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('massive','residential',0,'Starter',80.0,10.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "unitRate": 8.0, "originalPrice": 80.0, "originalUnitRate": 8.0, "bandwidth": "10 GB", "billing": "monthly", "period": "/month", "discountSource": null, "url": "https://www.joinmassive.com/web-access", "source": "https://www.joinmassive.com/web-access", "checkedAt": "2026-11-12"}'::jsonb,false),
('massive','residential',1,'Builder',225.0,30.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "unitRate": 7.5, "originalPrice": 225.0, "originalUnitRate": 7.5, "bandwidth": "30 GB", "billing": "monthly", "period": "/month", "discountSource": null, "url": "https://www.joinmassive.com/web-access", "source": "https://www.joinmassive.com/web-access", "checkedAt": "2026-11-12"}'::jsonb,false),
('massive','residential',2,'Scaler',385.0,70.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "unitRate": 5.5, "originalPrice": 385.0, "originalUnitRate": 5.5, "bandwidth": "70 GB", "billing": "monthly", "period": "/month", "discountSource": null, "url": "https://www.joinmassive.com/web-access", "source": "https://www.joinmassive.com/web-access", "checkedAt": "2026-11-12"}'::jsonb,false),
('massive','residential',3,'Pro',490.0,100.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "unitRate": 4.9, "originalPrice": 490.0, "originalUnitRate": 4.9, "bandwidth": "100 GB", "billing": "monthly", "period": "/month", "discountSource": null, "url": "https://www.joinmassive.com/web-access", "source": "https://www.joinmassive.com/web-access", "checkedAt": "2026-11-12"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo) VALUES
('massive','residential','2026-11-12',8.0,4.9,'https://www.joinmassive.com/web-access',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
