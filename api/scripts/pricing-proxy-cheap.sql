-- Proxy-Cheap residential list prices, read on https://proxy-cheap.com/services/rotating-residential-proxies on 2026-11-11.
-- Pool: Rotating Residential (pay as you go, volume tiers).
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only Proxy-Cheap's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('proxy-cheap','Proxy-Cheap','proxy-cheap')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('proxy-cheap','residential','per_gb',1.5,0.78,4.5,'None',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='proxy-cheap' AND category='residential' AND position>=3;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('proxy-cheap','residential',0,'3 GB+',NULL,NULL,NULL,NULL,false,NULL,'{"pool": "Rotating Residential (pay as you go, volume tiers)", "unitRate": 1.5, "bandwidth": "3 GB+", "period": "pay as you go", "originalPrice": 2.99, "source": "https://proxy-cheap.com/services/rotating-residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxy-cheap','residential',1,'4 GB+',NULL,NULL,NULL,NULL,false,NULL,'{"pool": "Rotating Residential (pay as you go, volume tiers)", "unitRate": 1.2, "bandwidth": "4 GB+", "period": "pay as you go", "originalPrice": 2.99, "source": "https://proxy-cheap.com/services/rotating-residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false),
('proxy-cheap','residential',2,'51 GB+',NULL,NULL,NULL,NULL,false,NULL,'{"pool": "Rotating Residential (pay as you go, volume tiers)", "unitRate": 0.78, "bandwidth": "51 GB+", "period": "pay as you go", "originalPrice": 2.6, "source": "https://proxy-cheap.com/services/rotating-residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo)
VALUES ('proxy-cheap','residential','2026-11-11',1.5,0.78,'https://proxy-cheap.com/services/rotating-residential-proxies observed 2026-11-11',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
