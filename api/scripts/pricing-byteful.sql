-- Byteful residential list prices, read on https://byteful.com/proxy-service/residential-proxies on 2026-11-11.
-- Pool: Residential Proxies (pay per GB, rotating).
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only Byteful's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('byteful','Byteful','byteful')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('byteful','residential','per_gb',3.25,1.75,32.5,'1 GB free trial (residential proxy page; the pricing page FAQ says no free trial)',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='byteful' AND category='residential' AND position>=3;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('byteful','residential',0,'10 GB',32.5,10.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies (pay per GB, rotating)", "unitRate": 3.25, "bandwidth": "10 GB", "period": "one-time", "source": "https://byteful.com/proxy-service/residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false),
('byteful','residential',1,'25 GB',75.0,25.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies (pay per GB, rotating)", "unitRate": 3, "bandwidth": "25 GB", "period": "one-time", "source": "https://byteful.com/proxy-service/residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false),
('byteful','residential',2,'100 GB',275.0,100.0,NULL,NULL,false,NULL,'{"pool": "Residential Proxies (pay per GB, rotating)", "unitRate": 2.75, "bandwidth": "100 GB", "period": "one-time", "source": "https://byteful.com/proxy-service/residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo)
VALUES ('byteful','residential','2026-11-11',3.25,1.75,'https://byteful.com/proxy-service/residential-proxies observed 2026-11-11',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
