-- FleetProxy residential list prices, read on https://fleetproxy.io/pricing on 2026-11-11.
-- Pool: Rotating Residential.
-- Run as the catalog writer role after siteapi migrate (0003 adds site.pricing_history).
-- Only FleetProxy's residential catalog is updated; the provider row is added only if missing.
BEGIN;
INSERT INTO site.providers(slug,name,framework_provider) VALUES ('fleetproxy','FleetProxy','fleetproxy')
ON CONFLICT DO NOTHING;
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,is_demo)
VALUES ('fleetproxy','residential','per_gb',2.65,1.59,2.65,'50MB free trial included',false)
ON CONFLICT (provider_slug,category) DO UPDATE SET model=EXCLUDED.model,price_per_gb=EXCLUDED.price_per_gb,
 price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,min_commit=EXCLUDED.min_commit,trial=EXCLUDED.trial,
 is_demo=false,updated_at=now();
DELETE FROM site.pricing_plans WHERE provider_slug='fleetproxy' AND category='residential' AND position>=7;
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,catalog,is_demo) VALUES
('fleetproxy','residential',0,'Starter 1 GB',2.65,1.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential", "unitRate": 2.65, "bandwidth": "1 GB", "period": "one-time", "source": "https://fleetproxy.io/pricing", "checkedAt": "2026-11-11"}'::jsonb,false),
('fleetproxy','residential',1,'5 GB',13.25,5.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential", "unitRate": 2.65, "bandwidth": "5 GB", "period": "one-time", "source": "https://fleetproxy.io/pricing", "checkedAt": "2026-11-11"}'::jsonb,false),
('fleetproxy','residential',2,'25 GB',62.94,25.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential", "unitRate": 2.52, "bandwidth": "25 GB", "period": "one-time", "originalPrice": 66.25, "source": "https://fleetproxy.io/pricing", "checkedAt": "2026-11-11"}'::jsonb,false),
('fleetproxy','residential',3,'50 GB',119.25,50.0,NULL,NULL,true,NULL,'{"pool": "Rotating Residential", "unitRate": 2.38, "bandwidth": "50 GB", "period": "one-time", "originalPrice": 132.5, "source": "https://fleetproxy.io/pricing", "checkedAt": "2026-11-11"}'::jsonb,false),
('fleetproxy','residential',4,'100 GB',212.0,100.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential", "unitRate": 2.12, "bandwidth": "100 GB", "period": "one-time", "originalPrice": 265, "source": "https://fleetproxy.io/pricing", "checkedAt": "2026-11-11"}'::jsonb,false),
('fleetproxy','residential',5,'250 GB',463.75,250.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential", "unitRate": 1.86, "bandwidth": "250 GB", "period": "one-time", "originalPrice": 662.5, "source": "https://fleetproxy.io/pricing", "checkedAt": "2026-11-11"}'::jsonb,false),
('fleetproxy','residential',6,'500 GB',795.0,500.0,NULL,NULL,false,NULL,'{"pool": "Rotating Residential", "unitRate": 1.59, "bandwidth": "500 GB", "period": "one-time", "originalPrice": 1325, "source": "https://fleetproxy.io/pricing", "checkedAt": "2026-11-11"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo)
VALUES ('fleetproxy','residential','2026-11-11',2.65,1.59,'https://fleetproxy.io/pricing observed 2026-11-11',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
