-- Infatica residential list prices, read on https://infatica.io/pricing/ on 2026-11-12.
-- Product: Residential rotating. Written by scripts/pricing_handoff.py (#927).
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only Infatica's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('infatica','Infatica','infatica')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('infatica','residential','per_gb',4.0,2.08,4.0,'$4.00 7 days trial',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='infatica' AND category='residential' AND position>=11;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('infatica','residential',0,'Pay As You Go',4.0,1.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "unitRate": 4.0, "originalPrice": 4.0, "originalUnitRate": 4.0, "bandwidth": "1 GB", "billing": "payg", "period": "one-time", "discountSource": null, "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',1,'25 GB monthly',96.0,25.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "unitRate": 3.84, "originalPrice": 96.0, "originalUnitRate": 3.84, "bandwidth": "25 GB", "billing": "monthly", "period": "/month", "discountSource": null, "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',2,'100 GB monthly',360.0,100.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "unitRate": 3.6, "originalPrice": 360.0, "originalUnitRate": 3.6, "bandwidth": "100 GB", "billing": "monthly", "period": "/month", "discountSource": null, "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',3,'241 GB monthly',700.0,241.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "unitRate": 2.9, "originalPrice": 700.0, "originalUnitRate": 2.9, "bandwidth": "241 GB", "billing": "monthly", "period": "/month", "discountSource": null, "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',4,'500 GB monthly',1350.0,500.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "unitRate": 2.7, "originalPrice": 1350.0, "originalUnitRate": 2.7, "bandwidth": "500 GB", "billing": "monthly", "period": "/month", "discountSource": null, "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',5,'1000 GB monthly',2600.0,1000.0,NULL,NULL,false,NULL,'{"pool": "Residential rotating", "unitRate": 2.6, "originalPrice": 2600.0, "originalUnitRate": 2.6, "bandwidth": "1 TB", "billing": "monthly", "period": "/month", "discountSource": null, "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',6,'300 GB annual',76.83,25.0,NULL,NULL,false,20.1,'{"pool": "Residential rotating", "unitRate": 3.07, "originalPrice": 96.0, "originalUnitRate": 3.84, "bandwidth": "25 GB", "billing": "annual", "period": "/month", "discountSource": "annual_billing", "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',7,'1200 GB annual',288.0,100.0,NULL,NULL,false,20.0,'{"pool": "Residential rotating", "unitRate": 2.88, "originalPrice": 360.0, "originalUnitRate": 3.6, "bandwidth": "100 GB", "billing": "annual", "period": "/month", "discountSource": "annual_billing", "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',8,'2892 GB annual',560.0,241.0,NULL,NULL,false,20.0,'{"pool": "Residential rotating", "unitRate": 2.32, "originalPrice": 700.0, "originalUnitRate": 2.9, "bandwidth": "241 GB", "billing": "annual", "period": "/month", "discountSource": "annual_billing", "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',9,'6000 GB annual',1080.0,500.0,NULL,NULL,false,20.0,'{"pool": "Residential rotating", "unitRate": 2.16, "originalPrice": 1350.0, "originalUnitRate": 2.7, "bandwidth": "500 GB", "billing": "annual", "period": "/month", "discountSource": "annual_billing", "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false),
('infatica','residential',10,'12000 GB annual',2080.0,1000.0,NULL,NULL,false,20.0,'{"pool": "Residential rotating", "unitRate": 2.08, "originalPrice": 2600.0, "originalUnitRate": 2.6, "bandwidth": "1 TB", "billing": "annual", "period": "/month", "discountSource": "annual_billing", "url": "https://infatica.io/pricing/", "source": "https://infatica.io/pricing/", "checkedAt": "2026-11-12"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo) VALUES
('infatica','residential','2026-11-11',4.0,2.6,'https://infatica.io/pricing/',false),
('infatica','residential','2026-11-12',4.0,2.08,'https://infatica.io/pricing/',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
