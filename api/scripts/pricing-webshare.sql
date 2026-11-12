-- Webshare residential list prices, read on https://www.webshare.io/pricing on 2026-11-12.
-- Product: Rotating residential. Written by scripts/pricing_handoff.py (#927).
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only Webshare's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('webshare','Webshare','webshare')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('webshare','residential','per_gb',3.5,0.98,3.5,'None',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='webshare' AND category='residential' AND position>=14;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('webshare','residential',0,'1 GB monthly',3.5,1.0,NULL,'CYBER26',false,NULL,'{"pool": "Rotating residential", "unitRate": 3.5, "originalPrice": 3.5, "originalUnitRate": 3.5, "bandwidth": "1 GB", "billing": "monthly", "period": "/month", "discountSource": null, "url": "https://www.webshare.io/pricing", "source": "https://www.webshare.io/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('webshare','residential',1,'10 GB monthly',27.5,10.0,NULL,'CYBER26',false,21.4,'{"pool": "Rotating residential", "unitRate": 2.75, "originalPrice": 35.0, "originalUnitRate": 3.5, "bandwidth": "10 GB", "billing": "monthly", "period": "/month", "discountSource": "volume", "url": "https://www.webshare.io/pricing", "source": "https://www.webshare.io/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('webshare','residential',2,'25 GB monthly',65.0,25.0,NULL,'CYBER26',false,25.7,'{"pool": "Rotating residential", "unitRate": 2.6, "originalPrice": 87.5, "originalUnitRate": 3.5, "bandwidth": "25 GB", "billing": "monthly", "period": "/month", "discountSource": "volume", "url": "https://www.webshare.io/pricing", "source": "https://www.webshare.io/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('webshare','residential',3,'50 GB monthly',122.5,50.0,NULL,'CYBER26',false,30.0,'{"pool": "Rotating residential", "unitRate": 2.45, "originalPrice": 175.0, "originalUnitRate": 3.5, "bandwidth": "50 GB", "billing": "monthly", "period": "/month", "discountSource": "volume", "url": "https://www.webshare.io/pricing", "source": "https://www.webshare.io/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('webshare','residential',4,'100 GB monthly',225.0,100.0,NULL,'CYBER26',false,35.7,'{"pool": "Rotating residential", "unitRate": 2.25, "originalPrice": 350.0, "originalUnitRate": 3.5, "bandwidth": "100 GB", "billing": "monthly", "period": "/month", "discountSource": "volume", "url": "https://www.webshare.io/pricing", "source": "https://www.webshare.io/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('webshare','residential',5,'250 GB monthly',500.0,250.0,NULL,'CYBER26',false,42.9,'{"pool": "Rotating residential", "unitRate": 2.0, "originalPrice": 875.0, "originalUnitRate": 3.5, "bandwidth": "250 GB", "billing": "monthly", "period": "/month", "discountSource": "volume", "url": "https://www.webshare.io/pricing", "source": "https://www.webshare.io/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('webshare','residential',6,'500 GB monthly',875.0,500.0,NULL,'CYBER26',false,50.0,'{"pool": "Rotating residential", "unitRate": 1.75, "originalPrice": 1750.0, "originalUnitRate": 3.5, "bandwidth": "500 GB", "billing": "monthly", "period": "/month", "discountSource": "volume", "url": "https://www.webshare.io/pricing", "source": "https://www.webshare.io/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('webshare','residential',7,'1000 GB monthly',1500.0,1000.0,NULL,'CYBER26',false,57.1,'{"pool": "Rotating residential", "unitRate": 1.5, "originalPrice": 3500.0, "originalUnitRate": 3.5, "bandwidth": "1 TB", "billing": "monthly", "period": "/month", "discountSource": "volume", "url": "https://www.webshare.io/pricing", "source": "https://www.webshare.io/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('webshare','residential',8,'3000 GB monthly',4200.0,3000.0,NULL,'CYBER26',false,60.0,'{"pool": "Rotating residential", "unitRate": 1.4, "originalPrice": 10500.0, "originalUnitRate": 3.5, "bandwidth": "3 TB", "billing": "monthly", "period": "/month", "discountSource": "volume", "url": "https://www.webshare.io/pricing", "source": "https://www.webshare.io/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('webshare','residential',9,'1 GB yearly',2.45,1.0,NULL,'CYBER26',false,30.0,'{"pool": "Rotating residential", "unitRate": 2.45, "originalPrice": 3.5, "originalUnitRate": 3.5, "bandwidth": "1 GB", "billing": "annual", "period": "/month", "discountSource": "annual_billing", "url": "https://www.webshare.io/pricing", "source": "https://www.webshare.io/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('webshare','residential',10,'10 GB yearly',19.25,10.0,NULL,'CYBER26',false,44.9,'{"pool": "Rotating residential", "unitRate": 1.93, "originalPrice": 35.0, "originalUnitRate": 3.5, "bandwidth": "10 GB", "billing": "annual", "period": "/month", "discountSource": "annual_billing", "url": "https://www.webshare.io/pricing", "source": "https://www.webshare.io/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('webshare','residential',11,'25 GB yearly',45.5,25.0,NULL,'CYBER26',false,48.0,'{"pool": "Rotating residential", "unitRate": 1.82, "originalPrice": 87.5, "originalUnitRate": 3.5, "bandwidth": "25 GB", "billing": "annual", "period": "/month", "discountSource": "annual_billing", "url": "https://www.webshare.io/pricing", "source": "https://www.webshare.io/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('webshare','residential',12,'100 GB yearly',157.5,100.0,NULL,'CYBER26',false,54.9,'{"pool": "Rotating residential", "unitRate": 1.58, "originalPrice": 350.0, "originalUnitRate": 3.5, "bandwidth": "100 GB", "billing": "annual", "period": "/month", "discountSource": "annual_billing", "url": "https://www.webshare.io/pricing", "source": "https://www.webshare.io/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('webshare','residential',13,'3000 GB yearly',2940.0,3000.0,NULL,'CYBER26',false,72.0,'{"pool": "Rotating residential", "unitRate": 0.98, "originalPrice": 10500.0, "originalUnitRate": 3.5, "bandwidth": "3 TB", "billing": "annual", "period": "/month", "discountSource": "annual_billing", "url": "https://www.webshare.io/pricing", "source": "https://www.webshare.io/pricing", "checkedAt": "2026-11-12"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo) VALUES
('webshare','residential','2026-11-11',3.5,1.4,'https://www.webshare.io/residential-proxy',false),
('webshare','residential','2026-11-12',3.5,0.98,'https://www.webshare.io/pricing',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
