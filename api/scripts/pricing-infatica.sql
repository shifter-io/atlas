-- Infatica residential list prices, read on https://infatica.io/pricing/ on 2026-11-11.
-- Pool: Residential proxies (45M+ IPs, billed monthly).
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only Infatica's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('infatica','Infatica','infatica')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('infatica','residential','per_gb',4.0,2.6,96.0,'$4.00 7 days trial',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='infatica' AND category='residential' AND position>=6;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('infatica','residential',0,'Pay As You Go',NULL,NULL,NULL,NULL,false,NULL,'{"pool": "Residential proxies (45M+ IPs, billed monthly)", "unitRate": 4, "bandwidth": null, "period": "pay as you go", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('infatica','residential',1,'25 GB',96.0,25.0,NULL,NULL,false,NULL,'{"pool": "Residential proxies (45M+ IPs, billed monthly)", "unitRate": 3.84, "bandwidth": "25 GB", "period": "/month", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('infatica','residential',2,'100 GB',360.0,100.0,NULL,NULL,false,NULL,'{"pool": "Residential proxies (45M+ IPs, billed monthly)", "unitRate": 3.6, "bandwidth": "100 GB", "period": "/month", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('infatica','residential',3,'241 GB',700.0,241.0,NULL,NULL,false,NULL,'{"pool": "Residential proxies (45M+ IPs, billed monthly)", "unitRate": 2.9, "bandwidth": "241 GB", "period": "/month", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('infatica','residential',4,'500 GB',1350.0,500.0,NULL,NULL,false,NULL,'{"pool": "Residential proxies (45M+ IPs, billed monthly)", "unitRate": 2.7, "bandwidth": "500 GB", "period": "/month", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false),
('infatica','residential',5,'1000 GB',2600.0,1000.0,NULL,NULL,false,NULL,'{"pool": "Residential proxies (45M+ IPs, billed monthly)", "unitRate": 2.6, "bandwidth": "1 TB", "period": "/month", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-11"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo)
VALUES ('infatica','residential','2026-11-11',4.0,2.6,'https://infatica.io/pricing/ observed 2026-11-11',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
