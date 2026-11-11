-- Evomi residential list prices, read on https://evomi.com/product/premium-residential-proxies on 2026-11-11.
-- Pool: Premium Residential.
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only Evomi's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('evomi','Evomi','evomi')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('evomi','residential','per_gb',3.98,2.2,3.98,'Free trial for all users',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='evomi' AND category='residential' AND position>=5;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('evomi','residential',0,'Pay as you go',3.98,1.0,NULL,NULL,false,NULL,'{"pool": "Premium Residential", "unitRate": 3.98, "bandwidth": "1 GB", "period": "pay as you go", "source": "https://evomi.com/product/premium-residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false),
('evomi','residential',1,'Flex Plan',30.0,8.0,NULL,NULL,false,NULL,'{"pool": "Premium Residential", "unitRate": 3.75, "bandwidth": "8 GB", "period": "/month", "source": "https://evomi.com/product/premium-residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false),
('evomi','residential',2,'Pro Plan',90.0,25.0,NULL,NULL,false,NULL,'{"pool": "Premium Residential", "unitRate": 3.6, "bandwidth": "25 GB", "period": "/month", "source": "https://evomi.com/product/premium-residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false),
('evomi','residential',3,'Advanced Plan',170.0,50.0,NULL,NULL,true,NULL,'{"pool": "Premium Residential", "unitRate": 3.4, "bandwidth": "50 GB", "period": "/month", "source": "https://evomi.com/product/premium-residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false),
('evomi','residential',4,'Largest monthly plans',NULL,NULL,NULL,'as low as $2.20/GB, priced on request',false,NULL,'{"pool": "Premium Residential", "unitRate": 2.2, "bandwidth": null, "period": "/month", "source": "https://evomi.com/product/premium-residential-proxies", "checkedAt": "2026-11-11"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo)
VALUES ('evomi','residential','2026-11-11',3.98,2.2,'https://evomi.com/product/premium-residential-proxies observed 2026-11-11',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
