-- Oxylabs residential list prices, read on https://oxylabs.io/pricing/residential-proxy-pool on 2026-11-12.
-- Product: Residential rotating. Written by scripts/pricing_handoff.py (#927).
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only Oxylabs's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('oxylabs','Oxylabs','oxylabs')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('oxylabs','residential','per_gb',6.0,2.5,15.0,'$5 welcome credit (one-time, on registration)',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='oxylabs' AND category='residential' AND position>=4;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('oxylabs','residential',0,'Starter',15.0,2.5,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "unitRate": 6.0, "originalPrice": 15.0, "originalUnitRate": 6.0, "bandwidth": "2.5 GB", "billing": "monthly", "period": "/month", "discountSource": null, "url": "https://oxylabs.io/pricing/residential-proxy-pool", "source": "https://oxylabs.io/pricing/residential-proxy-pool", "checkedAt": "2026-11-12"}'::jsonb,false),
('oxylabs','residential',1,'Basic',100.0,20.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "unitRate": 5.0, "originalPrice": 100.0, "originalUnitRate": 5.0, "bandwidth": "20 GB", "billing": "monthly", "period": "/month", "discountSource": null, "url": "https://oxylabs.io/pricing/residential-proxy-pool", "source": "https://oxylabs.io/pricing/residential-proxy-pool", "checkedAt": "2026-11-12"}'::jsonb,false),
('oxylabs','residential',2,'Flexible',500.0,125.0,NULL,NULL,true,NULL,'{"pool": "Residential rotating", "unitRate": 4.0, "originalPrice": 500.0, "originalUnitRate": 4.0, "bandwidth": "125 GB", "billing": "monthly", "period": "/month", "discountSource": null, "url": "https://oxylabs.io/pricing/residential-proxy-pool", "source": "https://oxylabs.io/pricing/residential-proxy-pool", "checkedAt": "2026-11-12"}'::jsonb,false),
('oxylabs','residential',3,'Enterprise',2500.0,1000.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "unitRate": 2.5, "originalPrice": 2500.0, "originalUnitRate": 2.5, "bandwidth": "1 TB", "billing": "monthly", "period": "/month", "discountSource": null, "url": "https://oxylabs.io/pricing/residential-proxy-pool", "source": "https://oxylabs.io/pricing/residential-proxy-pool", "checkedAt": "2026-11-12"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo) VALUES
('oxylabs','residential','2026-11-12',6.0,2.5,'https://oxylabs.io/pricing/residential-proxy-pool',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
