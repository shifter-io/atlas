-- FleetProxy residential list prices, read on https://fleetproxy.io/pricing#rotating-residential on 2026-11-12.
-- Product: Residential rotating. Written by scripts/pricing_handoff.py (#927).
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
('fleetproxy','residential',0,'1 GB',2.65,1.0,NULL,'WELCOME35',false,NULL,'{"pool": "Residential rotating", "unitRate": 2.65, "originalPrice": 2.65, "originalUnitRate": 2.65, "bandwidth": "1 GB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://fleetproxy.io/pricing#rotating-residential", "source": "https://fleetproxy.io/pricing#rotating-residential", "checkedAt": "2026-11-12"}'::jsonb,false),
('fleetproxy','residential',1,'5 GB',13.25,5.0,NULL,'WELCOME35',false,NULL,'{"pool": "Residential rotating", "unitRate": 2.65, "originalPrice": 13.25, "originalUnitRate": 2.65, "bandwidth": "5 GB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://fleetproxy.io/pricing#rotating-residential", "source": "https://fleetproxy.io/pricing#rotating-residential", "checkedAt": "2026-11-12"}'::jsonb,false),
('fleetproxy','residential',2,'25 GB',62.94,25.0,NULL,'WELCOME35',false,5.0,'{"pool": "Residential rotating", "unitRate": 2.5176, "originalPrice": 66.25, "originalUnitRate": 2.65, "bandwidth": "25 GB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://fleetproxy.io/pricing#rotating-residential", "source": "https://fleetproxy.io/pricing#rotating-residential", "checkedAt": "2026-11-12"}'::jsonb,false),
('fleetproxy','residential',3,'50 GB',119.25,50.0,NULL,'WELCOME35',false,10.0,'{"pool": "Residential rotating", "unitRate": 2.385, "originalPrice": 132.5, "originalUnitRate": 2.65, "bandwidth": "50 GB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://fleetproxy.io/pricing#rotating-residential", "source": "https://fleetproxy.io/pricing#rotating-residential", "checkedAt": "2026-11-12"}'::jsonb,false),
('fleetproxy','residential',4,'100 GB',212.0,100.0,NULL,'WELCOME35',false,20.0,'{"pool": "Residential rotating", "unitRate": 2.12, "originalPrice": 265.0, "originalUnitRate": 2.65, "bandwidth": "100 GB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://fleetproxy.io/pricing#rotating-residential", "source": "https://fleetproxy.io/pricing#rotating-residential", "checkedAt": "2026-11-12"}'::jsonb,false),
('fleetproxy','residential',5,'250 GB',463.75,250.0,NULL,'WELCOME35',false,30.0,'{"pool": "Residential rotating", "unitRate": 1.855, "originalPrice": 662.5, "originalUnitRate": 2.65, "bandwidth": "250 GB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://fleetproxy.io/pricing#rotating-residential", "source": "https://fleetproxy.io/pricing#rotating-residential", "checkedAt": "2026-11-12"}'::jsonb,false),
('fleetproxy','residential',6,'500 GB',795.0,500.0,NULL,'WELCOME35',false,40.0,'{"pool": "Residential rotating", "unitRate": 1.59, "originalPrice": 1325.0, "originalUnitRate": 2.65, "bandwidth": "500 GB", "billing": "payg", "period": "one-time", "discountSource": "volume", "url": "https://fleetproxy.io/pricing#rotating-residential", "source": "https://fleetproxy.io/pricing#rotating-residential", "checkedAt": "2026-11-12"}'::jsonb,false)
ON CONFLICT (provider_slug,category,position) DO UPDATE SET
 name=EXCLUDED.name,price=EXCLUDED.price,gb=EXCLUDED.gb,ips=EXCLUDED.ips,
 note=EXCLUDED.note,recommended=EXCLUDED.recommended,discount_pct=EXCLUDED.discount_pct,
 catalog=EXCLUDED.catalog,is_demo=false,updated_at=now();
INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo) VALUES
('fleetproxy','residential','2026-11-12',2.65,1.59,'https://fleetproxy.io/pricing#rotating-residential',false)
ON CONFLICT (provider_slug,category,effective_from) DO UPDATE SET
 price_per_gb=EXCLUDED.price_per_gb,price_per_gb_at_scale=EXCLUDED.price_per_gb_at_scale,
 source=EXCLUDED.source,is_demo=false,updated_at=now();
COMMIT;
