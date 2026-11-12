-- Byteful residential list prices, read on https://byteful.com/pricing on 2026-11-12.
-- Product: Residential rotating. Written by scripts/pricing_handoff.py (#927).
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only Byteful's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('byteful','Byteful','byteful')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('byteful','residential','per_gb',3.25,1.75,3.25,'1 GB free trial (residential proxy page; the pricing page FAQ says no free trial)',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='byteful' AND category='residential' AND position>=15;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('byteful','residential',0,'1 GB',3.25,1.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "unitRate": 3.25, "originalPrice": 3.25, "originalUnitRate": 3.25, "bandwidth": "1 GB", "billing": "payg", "period": "one-time", "discountSource": null, "url": "https://byteful.com/pricing", "source": "https://byteful.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('byteful','residential',1,'2 GB',6.5,2.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "unitRate": 3.25, "originalPrice": 6.5, "originalUnitRate": 3.25, "bandwidth": "2 GB", "billing": "payg", "period": "one-time", "discountSource": null, "url": "https://byteful.com/pricing", "source": "https://byteful.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('byteful','residential',2,'5 GB',16.25,5.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "unitRate": 3.25, "originalPrice": 16.25, "originalUnitRate": 3.25, "bandwidth": "5 GB", "billing": "payg", "period": "one-time", "discountSource": null, "url": "https://byteful.com/pricing", "source": "https://byteful.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('byteful','residential',3,'10 GB',32.5,10.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "unitRate": 3.25, "originalPrice": 32.5, "originalUnitRate": 3.25, "bandwidth": "10 GB", "billing": "payg", "period": "one-time", "discountSource": null, "url": "https://byteful.com/pricing", "source": "https://byteful.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('byteful','residential',4,'25 GB',75.0,25.0,NULL,NULL,false,7.7,'{"pool": "Residential rotating", "unitRate": 3.0, "originalPrice": 81.25, "originalUnitRate": 3.25, "bandwidth": "25 GB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://byteful.com/pricing", "source": "https://byteful.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('byteful','residential',5,'50 GB',150.0,50.0,NULL,NULL,false,7.7,'{"pool": "Residential rotating", "unitRate": 3.0, "originalPrice": 162.5, "originalUnitRate": 3.25, "bandwidth": "50 GB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://byteful.com/pricing", "source": "https://byteful.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('byteful','residential',6,'100 GB',275.0,100.0,NULL,NULL,false,15.4,'{"pool": "Residential rotating", "unitRate": 2.75, "originalPrice": 325.0, "originalUnitRate": 3.25, "bandwidth": "100 GB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://byteful.com/pricing", "source": "https://byteful.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('byteful','residential',7,'250 GB',625.0,250.0,NULL,NULL,false,23.1,'{"pool": "Residential rotating", "unitRate": 2.5, "originalPrice": 812.5, "originalUnitRate": 3.25, "bandwidth": "250 GB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://byteful.com/pricing", "source": "https://byteful.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('byteful','residential',8,'500 GB',1125.0,500.0,NULL,NULL,false,30.8,'{"pool": "Residential rotating", "unitRate": 2.25, "originalPrice": 1625.0, "originalUnitRate": 3.25, "bandwidth": "500 GB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://byteful.com/pricing", "source": "https://byteful.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('byteful','residential',9,'1000 GB',1950.0,1000.0,NULL,NULL,false,40.0,'{"pool": "Residential rotating", "unitRate": 1.95, "originalPrice": 3250.0, "originalUnitRate": 3.25, "bandwidth": "1 TB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://byteful.com/pricing", "source": "https://byteful.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('byteful','residential',10,'1500 GB',2925.0,1500.0,NULL,NULL,false,40.0,'{"pool": "Residential rotating", "unitRate": 1.95, "originalPrice": 4875.0, "originalUnitRate": 3.25, "bandwidth": "1.5 TB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://byteful.com/pricing", "source": "https://byteful.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('byteful','residential',11,'2000 GB',3900.0,2000.0,NULL,NULL,false,40.0,'{"pool": "Residential rotating", "unitRate": 1.95, "originalPrice": 6500.0, "originalUnitRate": 3.25, "bandwidth": "2 TB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://byteful.com/pricing", "source": "https://byteful.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('byteful','residential',12,'3000 GB',5550.0,3000.0,NULL,NULL,false,43.1,'{"pool": "Residential rotating", "unitRate": 1.85, "originalPrice": 9750.0, "originalUnitRate": 3.25, "bandwidth": "3 TB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://byteful.com/pricing", "source": "https://byteful.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('byteful','residential',13,'4000 GB',7400.0,4000.0,NULL,NULL,false,43.1,'{"pool": "Residential rotating", "unitRate": 1.85, "originalPrice": 13000.0, "originalUnitRate": 3.25, "bandwidth": "4 TB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://byteful.com/pricing", "source": "https://byteful.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('byteful','residential',14,'5000 GB',8750.0,5000.0,NULL,NULL,false,46.2,'{"pool": "Residential rotating", "unitRate": 1.75, "originalPrice": 16250.0, "originalUnitRate": 3.25, "bandwidth": "5 TB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://byteful.com/pricing", "source": "https://byteful.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo) VALUES
('byteful','residential','2026-11-12',3.25,1.75,'https://byteful.com/pricing',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
