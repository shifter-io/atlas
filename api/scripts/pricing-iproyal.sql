-- IPRoyal residential list prices, read on https://iproyal.com/pricing/residential-proxies/ on 2026-11-11.
-- Pool: Royal Residential Proxies (subscription tab, 5% off).
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only IPRoyal's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('iproyal','IPRoyal','iproyal')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('iproyal','residential','per_gb',7.0,1.75,7.0,'None',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='iproyal' AND category='residential' AND position>=8;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('iproyal','residential',0,'1 GB',7.0,1.0,NULL,NULL,false,NULL,'{"pool": "Royal Residential Proxies (subscription tab, 5% off)", "unitRate": 7.0, "bandwidth": "1 GB", "period": "subscription", "originalPrice": 7.35, "source": "https://iproyal.com/pricing/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('iproyal','residential',1,'2 GB',11.9,2.0,NULL,NULL,false,NULL,'{"pool": "Royal Residential Proxies (subscription tab, 5% off)", "unitRate": 5.95, "bandwidth": "2 GB", "period": "subscription", "originalPrice": 12.5, "source": "https://iproyal.com/pricing/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('iproyal','residential',2,'10 GB',52.5,10.0,NULL,NULL,true,NULL,'{"pool": "Royal Residential Proxies (subscription tab, 5% off)", "unitRate": 5.25, "bandwidth": "10 GB", "period": "subscription", "originalPrice": 55.1, "source": "https://iproyal.com/pricing/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('iproyal','residential',3,'Custom plans',NULL,NULL,NULL,NULL,false,NULL,'{"pool": "Royal Residential Proxies (subscription tab, 5% off)", "unitRate": 1.75, "bandwidth": "10 TB+", "period": "subscription", "source": "https://iproyal.com/pricing/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('iproyal','residential',4,'Pay As You Go 1 GB',7.35,1.0,NULL,NULL,false,NULL,'{"pool": "Royal Residential Proxies (subscription tab, 5% off)", "unitRate": 7.35, "bandwidth": "1 GB", "period": "pay as you go", "source": "https://iproyal.com/pricing/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('iproyal','residential',5,'Pay As You Go 2 GB',12.5,2.0,NULL,NULL,false,NULL,'{"pool": "Royal Residential Proxies (subscription tab, 5% off)", "unitRate": 6.25, "bandwidth": "2 GB", "period": "pay as you go", "source": "https://iproyal.com/pricing/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('iproyal','residential',6,'Pay As You Go 10 GB',55.1,10.0,NULL,NULL,false,NULL,'{"pool": "Royal Residential Proxies (subscription tab, 5% off)", "unitRate": 5.51, "bandwidth": "10 GB", "period": "pay as you go", "source": "https://iproyal.com/pricing/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('iproyal','residential',7,'Pay As You Go custom plans',NULL,NULL,NULL,NULL,false,NULL,'{"pool": "Royal Residential Proxies (subscription tab, 5% off)", "unitRate": 1.84, "bandwidth": "10 TB+", "period": "pay as you go", "source": "https://iproyal.com/pricing/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo)
VALUES ('iproyal','residential','2026-11-11',7.0,1.75,'https://iproyal.com/pricing/residential-proxies/ observed 2026-11-11',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
