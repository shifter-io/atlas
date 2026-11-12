-- Evomi residential list prices, read on https://evomi.com/pricing on 2026-11-12.
-- Product: Core residential. Written by scripts/pricing_handoff.py (#927).
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only Evomi's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('evomi','Evomi','evomi')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('evomi','residential','per_gb',0.99,0.49,14.85,'Free trial for all users',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='evomi' AND category='residential' AND position>=3;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('evomi','residential',0,'Monthly plan 100 GB',49.0,100.0,NULL,NULL,false,NULL,'{"pool": "Core residential", "unitRate": 0.49, "originalPrice": 49.0, "originalUnitRate": 0.49, "bandwidth": "100 GB", "billing": "monthly", "period": "/month", "discountSource": null, "url": "https://evomi.com/pricing", "source": "https://evomi.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('evomi','residential',1,'Pay as you go (from 15 GB)',14.85,15.0,NULL,NULL,false,NULL,'{"pool": "Core residential", "unitRate": 0.99, "originalPrice": 14.85, "originalUnitRate": 0.99, "bandwidth": "15 GB", "billing": "payg", "period": "one-time", "discountSource": null, "url": "https://evomi.com/pricing", "source": "https://evomi.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false),
('evomi','residential',2,'Pay as you go (from 1,000 GB)',790.0,1000.0,NULL,NULL,false,NULL,'{"pool": "Core residential", "unitRate": 0.79, "originalPrice": 790.0, "originalUnitRate": 0.79, "bandwidth": "1 TB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://evomi.com/pricing", "source": "https://evomi.com/pricing", "checkedAt": "2026-11-12"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo) VALUES
('evomi','residential','2026-11-12',0.99,0.49,'https://evomi.com/pricing',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
