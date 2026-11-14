-- A category's trial as numbers (PRICING-CODES-1): what the buyer pays for it in US dollars (0
-- for a free trial or a credit), the credit it gives in US dollars (0 when it gives none), its
-- traffic in GB, its length in days (null: no time limit) and whether it needs a card (null:
-- unknown). Additions only: the English trial text stays until the website reads these.
-- A plan's audience and its pool's kinds go in site.pricing_plans.catalog (audience, poolKinds).
ALTER TABLE site.pricing
 ADD COLUMN trial_price double precision CHECK (trial_price >= 0),
 ADD COLUMN trial_credit double precision CHECK (trial_credit >= 0),
 ADD COLUMN trial_gb double precision CHECK (trial_gb > 0),
 ADD COLUMN trial_days integer CHECK (trial_days > 0),
 ADD COLUMN trial_needs_card boolean;
