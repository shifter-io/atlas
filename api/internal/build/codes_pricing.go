package build

// A category's trial and its plans' audience and pool kinds (PRICING-CODES-1):
// trialKind is read from the catalog's trial, a plan's audience from its
// catalog (site.pricing_plans.catalog), whose poolKinds are proxy_kind codes.

func init() {
	RegisterCodeSet(CodeSet{Name: "trial_kind", Source: CodesFromAPI, UsedIn: []string{"* **.trialKind"},
		Values: codes("none", "No trial.", "sales", "A trial through a sales contact.", "credit", "Free credit to spend.",
			"paid", "A paid trial or a refund window.", "card", "A free trial that needs a card.", "free", "A free trial.")})
	RegisterCodeSet(CodeSet{Name: "plan_audience", Source: CodesFromAPI, UsedIn: []string{"* **.pricing.plans[].audience"},
		Values: codes("all", "Every buyer.", "new_customers", "New customers only.")})
}
