-- Rayobyte residential list prices, read on https://rayobyte.com/products/residential-proxies/ on 2026-11-11.
-- Pool: Premium Residential (pay as you go).
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only Rayobyte's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('rayobyte','Rayobyte','rayobyte')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('rayobyte','residential','per_gb',3.5,0.5,3.5,'Free trial, no credit card',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='rayobyte' AND category='residential' AND position>=5;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('rayobyte','residential',0,'1 to 49 GB',NULL,NULL,NULL,NULL,false,NULL,'{"pool": "Premium Residential (pay as you go)", "unitRate": 3.5, "bandwidth": "1 to 49 GB", "period": "pay as you go", "source": "https://rayobyte.com/products/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('rayobyte','residential',1,'50 to 249 GB',NULL,NULL,NULL,NULL,false,NULL,'{"pool": "Premium Residential (pay as you go)", "unitRate": 2.0, "bandwidth": "50 to 249 GB", "period": "pay as you go", "source": "https://rayobyte.com/products/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('rayobyte','residential',2,'250 to 999 GB',NULL,NULL,NULL,NULL,false,NULL,'{"pool": "Premium Residential (pay as you go)", "unitRate": 1.5, "bandwidth": "250 to 999 GB", "period": "pay as you go", "source": "https://rayobyte.com/products/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('rayobyte','residential',3,'1,000 to 4,999 GB',NULL,NULL,NULL,NULL,false,NULL,'{"pool": "Premium Residential (pay as you go)", "unitRate": 0.7, "bandwidth": "1,000 to 4,999 GB", "period": "pay as you go", "source": "https://rayobyte.com/products/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false),
('rayobyte','residential',4,'Enterprise',NULL,NULL,NULL,NULL,false,NULL,'{"pool": "Premium Residential (pay as you go)", "unitRate": 0.5, "bandwidth": "5,000 GB+", "period": "contact sales", "source": "https://rayobyte.com/products/residential-proxies/", "checkedAt": "2026-11-11"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo)
VALUES ('rayobyte','residential','2026-11-11',3.5,0.5,'https://rayobyte.com/products/residential-proxies/ observed 2026-11-11',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
