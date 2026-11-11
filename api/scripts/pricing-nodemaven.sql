-- NodeMaven residential list prices, read on https://nodemaven.com/pricing/ on 2026-11-11.
-- Pool: Residential + Mobile, rotating (one shared plan), billed monthly.
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only NodeMaven's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('nodemaven','NodeMaven','nodemaven')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('nodemaven','residential','per_gb',4.25,2.2,8.5,'Paid trial: $3.50 for 750 MB',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='nodemaven' AND category='residential' AND position>=18;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('nodemaven','residential',0,'2 GB',8.5,2.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 4.25, "bandwidth": "2 GB", "period": "/month", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('nodemaven','residential',1,'8 GB',32.0,8.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 4, "bandwidth": "8 GB", "period": "/month", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('nodemaven','residential',2,'20 GB',75.0,20.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 3.75, "bandwidth": "20 GB", "period": "/month", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('nodemaven','residential',3,'40 GB',138.0,40.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 3.45, "bandwidth": "40 GB", "period": "/month", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('nodemaven','residential',4,'55 GB',175.0,55.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 3.18182, "bandwidth": "55 GB", "period": "/month", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('nodemaven','residential',5,'100 GB',300.0,100.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 3, "bandwidth": "100 GB", "period": "/month", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('nodemaven','residential',6,'200 GB',570.0,200.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 2.85, "bandwidth": "200 GB", "period": "/month", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('nodemaven','residential',7,'450 GB',1170.0,450.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 2.6, "bandwidth": "450 GB", "period": "/month", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('nodemaven','residential',8,'1000 GB',2200.0,1000.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 2.2, "bandwidth": "1 TB", "period": "/month", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('nodemaven','residential',9,'Pay as you go 2 GB',10.0,2.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 5, "bandwidth": "2 GB", "period": "pay as you go", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('nodemaven','residential',10,'Pay as you go 8 GB',39.0,8.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 4.875, "bandwidth": "8 GB", "period": "pay as you go", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('nodemaven','residential',11,'Pay as you go 20 GB',90.0,20.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 4.5, "bandwidth": "20 GB", "period": "pay as you go", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('nodemaven','residential',12,'Pay as you go 40 GB',164.0,40.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 4.1, "bandwidth": "40 GB", "period": "pay as you go", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('nodemaven','residential',13,'Pay as you go 55 GB',205.0,55.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 3.72727, "bandwidth": "55 GB", "period": "pay as you go", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('nodemaven','residential',14,'Pay as you go 100 GB',345.0,100.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 3.45, "bandwidth": "100 GB", "period": "pay as you go", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('nodemaven','residential',15,'Pay as you go 200 GB',650.0,200.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 3.25, "bandwidth": "200 GB", "period": "pay as you go", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('nodemaven','residential',16,'Pay as you go 450 GB',1315.0,450.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 2.92222, "bandwidth": "450 GB", "period": "pay as you go", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('nodemaven','residential',17,'Pay as you go 1000 GB',2450.0,1000.0,NULL,NULL,false,NULL,'{"pool": "Residential + Mobile, rotating (one shared plan), billed monthly", "unitRate": 2.45, "bandwidth": "1 TB", "period": "pay as you go", "source": "https://nodemaven.com/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo)
VALUES ('nodemaven','residential','2026-11-11',4.25,2.2,'https://nodemaven.com/pricing/ observed 2026-11-11',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
