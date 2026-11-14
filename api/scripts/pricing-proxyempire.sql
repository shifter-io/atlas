-- ProxyEmpire residential list prices, read on https://proxyempire.io/pricing/ on 2026-11-12.
-- Product: Rotating residential. Written by scripts/pricing_handoff.py (#927).
-- Run as the catalog writer role after siteapi migrate (0003 and 0005 add what it writes).
-- Only ProxyEmpire's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('proxyempire','ProxyEmpire','proxyempire')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,trial_price,trial_credit,trial_gb,trial_days,trial_needs_card,is_demo)
VALUES ('proxyempire','residential','per_gb',3.5,1.5,3.5,'$1.97 trial, 150 MB, no expiry',1.97,0.0,0.15,NULL,NULL,false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 trial_price=EXCLUDED.trial_price,trial_credit=EXCLUDED.trial_credit,trial_gb=EXCLUDED.trial_gb,
 trial_days=EXCLUDED.trial_days,trial_needs_card=EXCLUDED.trial_needs_card,is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='proxyempire' AND category='residential' AND position>=8;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('proxyempire','residential',0,NULL,3.5,1.0,NULL,NULL,false,50.0,'{"pool": null, "poolKinds": ["residential"], "unitRate": 3.5, "originalPrice": 7.0, "originalUnitRate": 7.0, "billing": "payg", "audience": "all", "discountSource": "auto_coupon", "url": "https://proxyempire.io/pricing/", "source": "https://proxyempire.io/pricing/", "checkedAt": "2026-11-12", "code": "EMPIRE50"}'::jsonb,false),
('proxyempire','residential',1,'Starter',20.0,7.0,NULL,NULL,false,50.1,'{"pool": null, "poolKinds": ["residential"], "unitRate": 2.85, "originalPrice": 40.0, "originalUnitRate": 5.71, "billing": "monthly", "audience": "all", "discountSource": "auto_coupon", "url": "https://proxyempire.io/pricing/", "source": "https://proxyempire.io/pricing/", "checkedAt": "2026-11-12", "code": "EMPIRE50"}'::jsonb,false),
('proxyempire','residential',2,'Hobby',75.0,28.0,NULL,NULL,false,50.2,'{"pool": null, "poolKinds": ["residential"], "unitRate": 2.67, "originalPrice": 150.0, "originalUnitRate": 5.36, "billing": "monthly", "audience": "all", "discountSource": "auto_coupon", "url": "https://proxyempire.io/pricing/", "source": "https://proxyempire.io/pricing/", "checkedAt": "2026-11-12", "code": "EMPIRE50"}'::jsonb,false),
('proxyempire','residential',3,'Startup',150.0,60.0,NULL,NULL,false,50.0,'{"pool": null, "poolKinds": ["residential"], "unitRate": 2.5, "originalPrice": 300.0, "originalUnitRate": 5.0, "billing": "monthly", "audience": "all", "discountSource": "auto_coupon", "url": "https://proxyempire.io/pricing/", "source": "https://proxyempire.io/pricing/", "checkedAt": "2026-11-12", "code": "EMPIRE50"}'::jsonb,false),
('proxyempire','residential',4,'Business',300.0,135.0,NULL,NULL,false,50.0,'{"pool": null, "poolKinds": ["residential"], "unitRate": 2.22, "originalPrice": 600.0, "originalUnitRate": 4.44, "billing": "monthly", "audience": "all", "discountSource": "auto_coupon", "url": "https://proxyempire.io/pricing/", "source": "https://proxyempire.io/pricing/", "checkedAt": "2026-11-12", "code": "EMPIRE50"}'::jsonb,false),
('proxyempire','residential',5,'Company',625.0,315.0,NULL,NULL,false,50.1,'{"pool": null, "poolKinds": ["residential"], "unitRate": 1.98, "originalPrice": 1250.0, "originalUnitRate": 3.97, "billing": "monthly", "audience": "all", "discountSource": "auto_coupon", "url": "https://proxyempire.io/pricing/", "source": "https://proxyempire.io/pricing/", "checkedAt": "2026-11-12", "code": "EMPIRE50"}'::jsonb,false),
('proxyempire','residential',6,'Corporation',875.0,500.0,NULL,NULL,false,50.0,'{"pool": null, "poolKinds": ["residential"], "unitRate": 1.75, "originalPrice": 1750.0, "originalUnitRate": 3.5, "billing": "monthly", "audience": "all", "discountSource": "auto_coupon", "url": "https://proxyempire.io/pricing/", "source": "https://proxyempire.io/pricing/", "checkedAt": "2026-11-12", "code": "EMPIRE50"}'::jsonb,false),
('proxyempire','residential',7,'Enterprise',1500.0,1000.0,NULL,NULL,false,50.0,'{"pool": null, "poolKinds": ["residential"], "unitRate": 1.5, "originalPrice": 3000.0, "originalUnitRate": 3.0, "billing": "monthly", "audience": "all", "discountSource": "auto_coupon", "url": "https://proxyempire.io/pricing/", "source": "https://proxyempire.io/pricing/", "checkedAt": "2026-11-12", "code": "EMPIRE50"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo) VALUES
('proxyempire','residential','2026-11-12',3.5,1.5,'https://proxyempire.io/pricing/',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
