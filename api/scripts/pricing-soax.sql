-- SOAX residential list prices, read on https://soax.com/pricing on 2026-11-11.
-- Pool: Residential network, Tier 1 rates (for example the US); one rate across residential and mobile.
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only SOAX's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('soax','SOAX','soax')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('soax','residential','per_gb',5.0,0.85,0.0,'None',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='soax' AND category='residential' AND position>=5;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('soax','residential',0,'Sandbox',0.0,NULL,NULL,'no subscription fee and no included credits',false,NULL,'{"pool": "Residential network, Tier 1 rates (for example the US); one rate across residential and mobile", "unitRate": 5, "bandwidth": null, "period": "no subscription fee, top-up credits", "source": "https://soax.com/pricing", "checkedAt": "2026-11-11"}'::jsonb,false),
('soax','residential',1,'Builder',200.0,NULL,NULL,'200 credits included; Tier 2 $2.25/GB, Tier 3 $1.20/GB',false,NULL,'{"pool": "Residential network, Tier 1 rates (for example the US); one rate across residential and mobile", "unitRate": 3, "bandwidth": null, "period": "/month", "source": "https://soax.com/pricing", "checkedAt": "2026-11-11"}'::jsonb,false),
('soax','residential',2,'Team',500.0,NULL,NULL,'500 credits included; Tier 2 $1.65/GB, Tier 3 $0.90/GB',false,NULL,'{"pool": "Residential network, Tier 1 rates (for example the US); one rate across residential and mobile", "unitRate": 2.2, "bandwidth": null, "period": "/month", "source": "https://soax.com/pricing", "checkedAt": "2026-11-11"}'::jsonb,false),
('soax','residential',3,'Scale',1500.0,NULL,NULL,'1,500 credits included; Tier 2 $1.15/GB, Tier 3 $0.60/GB',false,NULL,'{"pool": "Residential network, Tier 1 rates (for example the US); one rate across residential and mobile", "unitRate": 1.5, "bandwidth": null, "period": "/month", "source": "https://soax.com/pricing", "checkedAt": "2026-11-11"}'::jsonb,false),
('soax','residential',4,'Enterprise',3000.0,NULL,NULL,'from $0.85/GB; 3,000 credits included; Tier 2 from $0.55/GB, Tier 3 from $0.35/GB',false,NULL,'{"pool": "Residential network, Tier 1 rates (for example the US); one rate across residential and mobile", "unitRate": 0.85, "bandwidth": null, "period": "/month", "source": "https://soax.com/pricing", "checkedAt": "2026-11-11"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo)
VALUES ('soax','residential','2026-11-11',5.0,0.85,'https://soax.com/pricing observed 2026-11-11',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
