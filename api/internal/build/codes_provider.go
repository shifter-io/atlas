package build

// A network's use cases and a community run's rejection reasons
// (PROVIDER-CODES-1): the catalog's site.providers.use_cases, a review's
// useCase and a run's rejectionReason hold these codes (the review's and the
// run's as text until #991).

// UseCases is the use_case set: what a network is good for.
var UseCases = codes(
	"web_scraping", "Collecting data from websites.",
	"price_monitoring", "Watching prices and assortments.",
	"ad_verification", "Checking where and how ads appear.",
	"seo_monitoring", "Tracking search engine optimization.",
	"serp_tracking", "Tracking search result pages.",
	"brand_protection", "Finding misuse of a brand.",
	"review_monitoring", "Watching customer reviews.",
	"market_research", "Studying markets and competitors.",
	"ai_training_data", "Collecting data to train AI models.",
	"ai_agents", "Letting AI agents browse the web.",
	"travel_fares", "Collecting travel fares.",
	"social_media", "Managing social media accounts.",
	"multi_accounting", "Running many accounts at once.",
	"cybersecurity", "Security research and testing.",
	"localized_testing", "Seeing content as people in other places see it.",
	"uptime_monitoring", "Checking that websites are up and fast.",
	"anonymous_browsing", "Browsing without revealing one's address.")

func init() {
	RegisterCodeSet(CodeSet{Name: "use_case", Source: CodesFromAPI, Values: UseCases,
		UsedIn: []string{"* **.useCases[]", "* **.reviews[].useCase", "/v1/networks/{slug}/reviews rows[].useCase"}})
	RegisterCodeSet(CodeSet{Name: "run_rejection_reason", Source: CodesFromAPI,
		UsedIn: []string{"* **.communityRuns[].rejectionReason", "/v1/networks/{slug}/runs rows[].rejectionReason"},
		Values: codes("too_few_requests", "Too few requests.", "collector_outdated", "The collector was out of date.",
			"duplicate", "A copy of another run.", "inconsistent_results", "Results that disagree with each other.",
			"other", "Another reason.")})
}
