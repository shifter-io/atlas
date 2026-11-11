-- ProxyEmpire residential list prices, read on https://proxyempire.io/pricing/ on 2026-11-11.
-- Pool: Rotating Residential.
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only ProxyEmpire's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('proxyempire','ProxyEmpire','proxyempire')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('proxyempire','residential','per_gb',3.5,0.75,3.5,'$1.97 trial, 150 MB, no expiry',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='proxyempire' AND category='residential' AND position>=9;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('proxyempire','residential',0,'Pay As You Go',3.5,1.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential", "unitRate": 3.5, "bandwidth": "1 GB", "period": "pay as you go", "originalPrice": 7, "source": "https://proxyempire.io/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxyempire','residential',1,'Starter',20.0,7.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential", "unitRate": 2.85, "bandwidth": "7 GB", "period": "/month", "originalPrice": 40, "source": "https://proxyempire.io/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxyempire','residential',2,'Hobby',75.0,28.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential", "unitRate": 2.67, "bandwidth": "28 GB", "period": "/month", "originalPrice": 150, "source": "https://proxyempire.io/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxyempire','residential',3,'Startup',150.0,60.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential", "unitRate": 2.5, "bandwidth": "60 GB", "period": "/month", "originalPrice": 300, "source": "https://proxyempire.io/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxyempire','residential',4,'Business',300.0,135.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential", "unitRate": 2.22, "bandwidth": "135 GB", "period": "/month", "originalPrice": 600, "source": "https://proxyempire.io/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxyempire','residential',5,'Company',625.0,315.0,NULL,NULL,true,NULL,'{"pool": "Rotating Residential", "unitRate": 1.98, "bandwidth": "315 GB", "period": "/month", "originalPrice": 1250, "source": "https://proxyempire.io/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxyempire','residential',6,'Corporation',875.0,500.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential", "unitRate": 1.75, "bandwidth": "500 GB", "period": "/month", "originalPrice": 1750, "source": "https://proxyempire.io/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxyempire','residential',7,'Enterprise',1500.0,1000.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential", "unitRate": 1.5, "bandwidth": "1 TB", "period": "/month", "originalPrice": 3000, "source": "https://proxyempire.io/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxyempire','residential',8,'Custom Plans',NULL,NULL,NULL,NULL,false,NULL,'{"pool": "Rotating Residential", "unitRate": 0.75, "bandwidth": "5 TB+", "period": "custom, contact sales", "source": "https://proxyempire.io/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo)
VALUES ('proxyempire','residential','2026-11-11',3.5,0.75,'https://proxyempire.io/pricing/ observed 2026-11-11',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
