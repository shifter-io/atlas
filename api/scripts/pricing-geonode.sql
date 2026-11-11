-- Geonode residential list prices, read on https://geonode.com/residential-proxies on 2026-11-11.
-- Pool: Residential Proxies (subscription tab, -10%).
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only Geonode's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('geonode','Geonode','geonode')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('geonode','residential','per_gb',0.792,0.27,7.92,'$5 trial includes 10GB for 3 days',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='geonode' AND category='residential' AND position>=10;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('geonode','residential',0,'10 GB Plan',7.92,10.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies (subscription tab, -10%)", "unitRate": 0.792, "bandwidth": "10 GB", "period": "/month", "source": "https://geonode.com/residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false),
('geonode','residential',1,'25 GB Plan',18.45,25.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies (subscription tab, -10%)", "unitRate": 0.738, "bandwidth": "25 GB", "period": "/month", "source": "https://geonode.com/residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false),
('geonode','residential',2,'50 GB Plan',34.65,50.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies (subscription tab, -10%)", "unitRate": 0.693, "bandwidth": "50 GB", "period": "/month", "source": "https://geonode.com/residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false),
('geonode','residential',3,'100 GB Plan',64.8,100.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies (subscription tab, -10%)", "unitRate": 0.648, "bandwidth": "100 GB", "period": "/month", "source": "https://geonode.com/residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false),
('geonode','residential',4,'250 GB Plan',148.5,250.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies (subscription tab, -10%)", "unitRate": 0.594, "bandwidth": "250 GB", "period": "/month", "source": "https://geonode.com/residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false),
('geonode','residential',5,'500 GB Plan',274.5,500.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies (subscription tab, -10%)", "unitRate": 0.549, "bandwidth": "500 GB", "period": "/month", "source": "https://geonode.com/residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false),
('geonode','residential',6,'1 TB Plan',500.0,1000.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies (subscription tab, -10%)", "unitRate": 0.5, "bandwidth": "1 TB", "period": "/month", "source": "https://geonode.com/residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false),
('geonode','residential',7,'5 TB Plan',2160.0,5000.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies (subscription tab, -10%)", "unitRate": 0.432, "bandwidth": "5 TB", "period": "/month", "source": "https://geonode.com/residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false),
('geonode','residential',8,'100 TB Plan',27000.0,100000.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies (subscription tab, -10%)", "unitRate": 0.27, "bandwidth": "100 TB", "period": "/month", "source": "https://geonode.com/residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false),
('geonode','residential',9,'Pay as you go 10 GB',8.8,10.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies (subscription tab, -10%)", "unitRate": 0.88, "bandwidth": "10 GB", "period": "pay as you go", "source": "https://geonode.com/residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo)
VALUES ('geonode','residential','2026-11-11',0.792,0.27,'https://geonode.com/residential-proxies observed 2026-11-11',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
