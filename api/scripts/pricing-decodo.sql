-- Decodo residential list prices, read on https://decodo.com/proxies/residential-proxies/pricing on 2026-11-11.
-- Pool: Residential Proxies.
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only Decodo's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('decodo','Decodo','decodo')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('decodo','residential','per_gb',4.0,2.0,4.0,'3-day free trial (100 MB)',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='decodo' AND category='residential' AND position>=8;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('decodo','residential',0,'Pay as you go',4.0,1.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies", "unitRate": 4, "bandwidth": "1 GB", "period": "pay as you go", "source": "https://decodo.com/proxies/residential-proxies/pricing", "checkedAt": "2026-11-11"}'::jsonb,false),
('decodo','residential',1,'3 GB',11.25,3.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies", "unitRate": 3.75, "bandwidth": "3 GB", "period": "/month", "source": "https://decodo.com/proxies/residential-proxies/pricing", "checkedAt": "2026-11-11"}'::jsonb,false),
('decodo','residential',2,'10 GB',35.0,10.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies", "unitRate": 3.5, "bandwidth": "10 GB", "period": "/month", "source": "https://decodo.com/proxies/residential-proxies/pricing", "checkedAt": "2026-11-11"}'::jsonb,false),
('decodo','residential',3,'25 GB',81.25,25.0,NULL,NULL,true,NULL,'{"pool": "Residential Proxies", "unitRate": 3.25, "bandwidth": "25 GB", "period": "/month", "source": "https://decodo.com/proxies/residential-proxies/pricing", "checkedAt": "2026-11-11"}'::jsonb,false),
('decodo','residential',4,'50 GB',150.0,50.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies", "unitRate": 3, "bandwidth": "50 GB", "period": "/month", "source": "https://decodo.com/proxies/residential-proxies/pricing", "checkedAt": "2026-11-11"}'::jsonb,false),
('decodo','residential',5,'100 GB',275.0,100.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies", "unitRate": 2.75, "bandwidth": "100 GB", "period": "/month", "source": "https://decodo.com/proxies/residential-proxies/pricing", "checkedAt": "2026-11-11"}'::jsonb,false),
('decodo','residential',6,'Enterprise 250 GB',625.0,250.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies", "unitRate": 2.5, "bandwidth": "250 GB", "period": "/month", "source": "https://decodo.com/proxies/residential-proxies/pricing", "checkedAt": "2026-11-11"}'::jsonb,false),
('decodo','residential',7,'Enterprise 1000 GB',2000.0,1000.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies", "unitRate": 2, "bandwidth": "1 TB", "period": "/month", "source": "https://decodo.com/proxies/residential-proxies/pricing", "checkedAt": "2026-11-11"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo)
VALUES ('decodo','residential','2026-11-11',4.0,2.0,'https://decodo.com/proxies/residential-proxies/pricing observed 2026-11-11',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
