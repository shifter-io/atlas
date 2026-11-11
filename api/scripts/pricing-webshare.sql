-- Webshare residential list prices, read on https://www.webshare.io/residential-proxy on 2026-11-11.
-- Pool: Rotating Residential, billed monthly.
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only Webshare's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('webshare','Webshare','webshare')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('webshare','residential','per_gb',3.5,1.4,3.5,'None',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='webshare' AND category='residential' AND position>=5;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('webshare','residential',0,'1 GB',3.5,1.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential, billed monthly", "unitRate": 3.5, "bandwidth": "1 GB", "period": "/month", "source": "https://www.webshare.io/residential-proxy", "checkedAt": "2026-11-11"}'::jsonb,false),
('webshare','residential',1,'10 GB',27.5,10.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential, billed monthly", "unitRate": 2.75, "bandwidth": "10 GB", "period": "/month", "originalPrice": 35, "source": "https://www.webshare.io/residential-proxy", "checkedAt": "2026-11-11"}'::jsonb,false),
('webshare','residential',2,'25 GB',65.0,25.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential, billed monthly", "unitRate": 2.6, "bandwidth": "25 GB", "period": "/month", "originalPrice": 87.5, "source": "https://www.webshare.io/residential-proxy", "checkedAt": "2026-11-11"}'::jsonb,false),
('webshare','residential',3,'100 GB',225.0,100.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential, billed monthly", "unitRate": 2.25, "bandwidth": "100 GB", "period": "/month", "originalPrice": 350, "source": "https://www.webshare.io/residential-proxy", "checkedAt": "2026-11-11"}'::jsonb,false),
('webshare','residential',4,'3,000 GB',4200.0,3000.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential, billed monthly", "unitRate": 1.4, "bandwidth": "3 TB", "period": "/month", "source": "https://www.webshare.io/residential-proxy", "checkedAt": "2026-11-11"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo)
VALUES ('webshare','residential','2026-11-11',3.5,1.4,'https://www.webshare.io/residential-proxy observed 2026-11-11',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
