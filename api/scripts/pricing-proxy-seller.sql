-- Proxy-Seller residential list prices, read on https://proxy-seller.com/residential-proxies/ on 2026-11-11.
-- Pool: Residential proxies (rotating, per GB).
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only Proxy-Seller's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('proxy-seller','Proxy-Seller','proxy-seller')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('proxy-seller','residential','per_gb',3.5,1.45,3.5,'None',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='proxy-seller' AND category='residential' AND position>=12;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('proxy-seller','residential',0,'Test it out',3.5,1.0,NULL,NULL,false,NULL,'{"pool": "Residential proxies (rotating, per GB)", "unitRate": 3.5, "bandwidth": "1 GB", "period": "30 days", "source": "https://proxy-seller.com/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxy-seller','residential',1,'3 GB',9.0,3.0,NULL,NULL,false,NULL,'{"pool": "Residential proxies (rotating, per GB)", "unitRate": 3.0, "bandwidth": "3 GB", "period": "30 days", "source": "https://proxy-seller.com/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxy-seller','residential',2,'10 GB',27.0,10.0,NULL,NULL,false,NULL,'{"pool": "Residential proxies (rotating, per GB)", "unitRate": 2.7, "bandwidth": "10 GB", "period": "30 days", "source": "https://proxy-seller.com/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxy-seller','residential',3,'25 GB',65.0,25.0,NULL,NULL,false,NULL,'{"pool": "Residential proxies (rotating, per GB)", "unitRate": 2.6, "bandwidth": "25 GB", "period": "30 days", "source": "https://proxy-seller.com/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxy-seller','residential',4,'50 GB',125.0,50.0,NULL,NULL,true,NULL,'{"pool": "Residential proxies (rotating, per GB)", "unitRate": 2.5, "bandwidth": "50 GB", "period": "30 days", "source": "https://proxy-seller.com/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxy-seller','residential',5,'100 GB',220.0,100.0,NULL,NULL,false,NULL,'{"pool": "Residential proxies (rotating, per GB)", "unitRate": 2.2, "bandwidth": "100 GB", "period": "30 days", "source": "https://proxy-seller.com/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxy-seller','residential',6,'200 GB',420.0,200.0,NULL,NULL,false,NULL,'{"pool": "Residential proxies (rotating, per GB)", "unitRate": 2.1, "bandwidth": "200 GB", "period": "30 days", "source": "https://proxy-seller.com/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxy-seller','residential',7,'300 GB',600.0,300.0,NULL,NULL,false,NULL,'{"pool": "Residential proxies (rotating, per GB)", "unitRate": 2.0, "bandwidth": "300 GB", "period": "30 days", "source": "https://proxy-seller.com/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxy-seller','residential',8,'500 GB',850.0,500.0,NULL,NULL,false,NULL,'{"pool": "Residential proxies (rotating, per GB)", "unitRate": 1.7, "bandwidth": "500 GB", "period": "30 days", "source": "https://proxy-seller.com/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxy-seller','residential',9,'750 GB',1125.0,750.0,NULL,NULL,false,NULL,'{"pool": "Residential proxies (rotating, per GB)", "unitRate": 1.5, "bandwidth": "750 GB", "period": "30 days", "source": "https://proxy-seller.com/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxy-seller','residential',10,'1000 GB',1450.0,1000.0,NULL,NULL,false,NULL,'{"pool": "Residential proxies (rotating, per GB)", "unitRate": 1.45, "bandwidth": "1 TB", "period": "30 days", "source": "https://proxy-seller.com/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxy-seller','residential',11,'Enterprise',NULL,NULL,NULL,'Custom solutions; the page says from $0.3/GB but No fixed rates, contact sales',false,NULL,'{"pool": "Residential proxies (rotating, per GB)", "unitRate": null, "bandwidth": null, "period": "30 days", "source": "https://proxy-seller.com/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo)
VALUES ('proxy-seller','residential','2026-11-11',3.5,1.45,'https://proxy-seller.com/residential-proxies/ observed 2026-11-11',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
