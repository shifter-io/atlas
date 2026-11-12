-- Proxy-Cheap residential list prices, read on https://www.proxy-cheap.com/pricing/rotating-residential-proxies on 2026-11-12.
-- Product: Rotating residential. Written by scripts/pricing_handoff.py (#927).
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
('proxy-cheap','residential',0,'3 GB+',4.5,3.0,NULL,NULL,false,49.8,'{"pool": "Rotating residential", "unitRate": 1.5, "originalPrice": 8.97, "originalUnitRate": 2.99, "bandwidth": "3 GB", "billing": "payg", "period": "one-time", "discountSource": "sale", "url": "https://www.proxy-cheap.com/pricing/rotating-residential-proxies", "source": "https://www.proxy-cheap.com/pricing/rotating-residential-proxies", "checkedAt": "2026-11-12"}'::jsonb,false),
('proxy-cheap','residential',1,'4 GB+',4.8,4.0,NULL,NULL,false,59.9,'{"pool": "Rotating residential", "unitRate": 1.2, "originalPrice": 11.96, "originalUnitRate": 2.99, "bandwidth": "4 GB", "billing": "payg", "period": "one-time", "discountSource": "sale", "url": "https://www.proxy-cheap.com/pricing/rotating-residential-proxies", "source": "https://www.proxy-cheap.com/pricing/rotating-residential-proxies", "checkedAt": "2026-11-12"}'::jsonb,false),
('proxy-cheap','residential',2,'51 GB+',39.78,51.0,NULL,NULL,false,70.0,'{"pool": "Rotating residential", "unitRate": 0.78, "originalPrice": 132.6, "originalUnitRate": 2.6, "bandwidth": "51 GB", "billing": "payg", "period": "one-time", "discountSource": "sale", "url": "https://www.proxy-cheap.com/pricing/rotating-residential-proxies", "source": "https://www.proxy-cheap.com/pricing/rotating-residential-proxies", "checkedAt": "2026-11-12"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo) VALUES
('proxy-cheap','residential','2026-11-11',1.5,0.78,'https://proxy-cheap.com/services/rotating-residential-proxies',false),
('proxy-cheap','residential','2026-11-12',1.5,0.78,'https://www.proxy-cheap.com/pricing/rotating-residential-proxies',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
