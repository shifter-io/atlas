package build

// A network's pricing, pool, reviews, runs and places, and /healthz
// (CODES-CATALOG-1).

func init() {
	RegisterCodeSet(CodeSet{Name: "pricing_model", Source: CodesFromAPI, UsedIn: []string{"* **.pricing.model"},
		Values: codes("per_gb", "Sold by the gigabyte.")})
	RegisterCodeSet(CodeSet{Name: "price_basis", Source: CodesFromAPI, UsedIn: []string{"* **.priceBasis"},
		Values: codes("first_200gb_plus", "The first plan of 200 GB or more.",
			"largest_below_200gb", "The largest plan below 200 GB.")})
	RegisterCodeSet(CodeSet{Name: "billing", Source: CodesFromAPI, UsedIn: []string{"* **.pricing.plans[].catalog.billing"},
		Values: codes("monthly", "Billed every month.", "payg", "Paid as you go, once.", "30_day", "Valid for 30 days.",
			"annual", "Billed every year.")})
	RegisterCodeSet(CodeSet{Name: "discount_source", Source: CodesFromAPI, UsedIn: []string{"* **.pricing.plans[].catalog.discountSource"},
		Values: codes("sale", "A sale.", "volume", "A volume discount.", "annual_billing", "Paying for a year.",
			"auto_coupon", "A coupon the shop applies by itself.", "subscription", "Subscribing instead of paying once.")})
	RegisterCodeSet(CodeSet{Name: "trial_kind", Source: CodesFromAPI, UsedIn: []string{"* **.trialKind"},
		Values: codes("none", "No trial.", "sales", "A trial through a sales contact.", "paid", "A paid trial or a refund window.",
			"card", "A free trial that needs a card.", "free", "A free trial.")})
	RegisterCodeSet(CodeSet{Name: "bill_unit", Source: CodesFromAPI, UsedIn: []string{"/v1/home insights.valueAll.unit"},
		Values: codes("usd_per_1k_successes", "US dollars per 1,000 successful requests.")})
	RegisterCodeSet(CodeSet{Name: "bill_weighting", Source: CodesFromAPI, UsedIn: []string{"/v1/home insights.valueAll.weighting"},
		Values: codes("equal", "Every target counts the same.")})
	RegisterCodeSet(CodeSet{Name: "overlap_label", Source: CodesFromAPI, UsedIn: []string{"/v1/overlap networks[].label"},
		Values: codes(LabelFullyExclusive, "No address shared with another network.",
			LabelMostlyExclusive, "Few addresses shared.", LabelMixed, "Many addresses shared.",
			LabelResale, "Most addresses shared: likely a reseller.")})
	RegisterCodeSet(CodeSet{Name: "pool_basis", Source: CodesFromAPI, UsedIn: []string{"/v1/overlap networks[].poolBasis"},
		Values: codes(PoolBasisOverlap, "The pool the overlap comparison measured.", PoolBasisWindow, "The window's unique addresses.")})
	RegisterCodeSet(CodeSet{Name: "duplication_basis", Source: CodesFromAPI,
		UsedIn: []string{"/v1/overlap market.basis", "/v1/home market.duplication.basis"},
		Values: codes(BasisKindFleet, "The kind's networks together.")})
	RegisterCodeSet(CodeSet{Name: "asn_kind", Source: CodesFromAPI, UsedIn: []string{"* **.asns[].kind"},
		Values: codes("eyeball", "A consumer network.", "hosting", "A hosting network.", "mobile", "A mobile network.",
			"business", "A business network.")})
	RegisterCodeSet(CodeSet{Name: "review_verification", Source: CodesFromAPI,
		UsedIn: []string{"* **.reviews[].verification", "/v1/networks/{slug}/reviews rows[].verification"},
		Values: codes("invoice_verified", "Verified by an invoice.", "run_verified", "Verified by a community run.",
			"unverified", "Not verified.", "pending", "In review.")})
	RegisterCodeSet(CodeSet{Name: "run_status", Source: CodesFromAPI,
		UsedIn: []string{"* **.communityRuns[].status", "/v1/networks/{slug}/runs rows[].status"},
		Values: codes("accepted", "Accepted.", "pending", "In review.", "rejected", "Rejected.")})
	RegisterCodeSet(CodeSet{Name: "place_dimension", Source: CodesFromAPI, UsedIn: []string{"/v1/networks/{slug}/places dimension"},
		Values: codes("country", "Countries.", "region", "Regions.", "city", "Cities.", "asn", "Networks (ASNs).",
			"city_asn", "Cities by network.")})
	RegisterCodeSet(CodeSet{Name: "health_status", Source: CodesFromAPI, UsedIn: []string{"/healthz status"},
		Values: codes("ok", "Serving.", "degraded", "Serving, with a problem.", "starting", "Not serving yet.",
			"unavailable", "Not serving.")})
}
