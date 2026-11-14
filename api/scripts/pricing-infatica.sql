-- Infatica residential list prices, read on https://infatica.io/pricing/ on 2026-11-12.
-- Product: Residential rotating. Written by scripts/pricing_handoff.py (#927).
-- Run as the catalog writer role after siteapi migrate (0003 and 0005 add what it writes).
-- Only Infatica's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('infatica','Infatica','infatica')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,trial_price,trial_credit,trial_gb,trial_days,trial_needs_card,is_demo)
VALUES ('infatica','residential','per_gb',4.0,2.08,4.0,'$4.00 7 days trial',4.0,0.0,NULL,7,NULL,false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 trial_price=EXCLUDED.trial_price,trial_credit=EXCLUDED.trial_credit,trial_gb=EXCLUDED.trial_gb,
 trial_days=EXCLUDED.trial_days,trial_needs_card=EXCLUDED.trial_needs_card,is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='infatica' AND category='residential' AND position>=11;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('infatica','residential',0,NULL,4.0,1.0,NULL,NULL,false,NULL,'{"pool": null, "poolKinds": ["residential"], "unitRate": 4.0, "originalPrice": 4.0, "originalUnitRate": 4.0, "billing": "payg", "audience": "all", "discountSource": null, "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',1,NULL,96.0,25.0,NULL,NULL,false,NULL,'{"pool": null, "poolKinds": ["residential"], "unitRate": 3.84, "originalPrice": 96.0, "originalUnitRate": 3.84, "billing": "monthly", "audience": "all", "discountSource": null, "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',2,NULL,360.0,100.0,NULL,NULL,false,NULL,'{"pool": null, "poolKinds": ["residential"], "unitRate": 3.6, "originalPrice": 360.0, "originalUnitRate": 3.6, "billing": "monthly", "audience": "all", "discountSource": null, "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',3,NULL,700.0,241.0,NULL,NULL,false,NULL,'{"pool": null, "poolKinds": ["residential"], "unitRate": 2.9, "originalPrice": 700.0, "originalUnitRate": 2.9, "billing": "monthly", "audience": "all", "discountSource": null, "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',4,NULL,1350.0,500.0,NULL,NULL,false,NULL,'{"pool": null, "poolKinds": ["residential"], "unitRate": 2.7, "originalPrice": 1350.0, "originalUnitRate": 2.7, "billing": "monthly", "audience": "all", "discountSource": null, "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',5,NULL,2600.0,1000.0,NULL,NULL,false,NULL,'{"pool": null, "poolKinds": ["residential"], "unitRate": 2.6, "originalPrice": 2600.0, "originalUnitRate": 2.6, "billing": "monthly", "audience": "all", "discountSource": null, "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',6,NULL,76.83,25.0,NULL,NULL,false,20.1,'{"pool": null, "poolKinds": ["residential"], "unitRate": 3.07, "originalPrice": 96.0, "originalUnitRate": 3.84, "billing": "annual", "audience": "all", "discountSource": "annual_billing", "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',7,NULL,288.0,100.0,NULL,NULL,false,20.0,'{"pool": null, "poolKinds": ["residential"], "unitRate": 2.88, "originalPrice": 360.0, "originalUnitRate": 3.6, "billing": "annual", "audience": "all", "discountSource": "annual_billing", "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',8,NULL,560.0,241.0,NULL,NULL,false,20.0,'{"pool": null, "poolKinds": ["residential"], "unitRate": 2.32, "originalPrice": 700.0, "originalUnitRate": 2.9, "billing": "annual", "audience": "all", "discountSource": "annual_billing", "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',9,NULL,1080.0,500.0,NULL,NULL,false,20.0,'{"pool": null, "poolKinds": ["residential"], "unitRate": 2.16, "originalPrice": 1350.0, "originalUnitRate": 2.7, "billing": "annual", "audience": "all", "discountSource": "annual_billing", "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',10,NULL,2080.0,1000.0,NULL,NULL,false,20.0,'{"pool": null, "poolKinds": ["residential"], "unitRate": 2.08, "originalPrice": 2600.0, "originalUnitRate": 2.6, "billing": "annual", "audience": "all", "discountSource": "annual_billing", "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo) VALUES
('infatica','residential','2026-11-12',4.0,2.08,'https://infatica.io/pricing/',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
