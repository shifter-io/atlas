package build

import "github.com/shifter-io/atlas/api/internal/model"

// The catalog's targets, scopes and ranking profiles, the engines, the
// outcomes and the scopes' geographic coverage (CODES-CATALOG-1).

func init() {
	RegisterCodeSet(CodeSet{Name: "target", Source: CodesFromCatalog, Catalog: targetCodes,
		From: "site.targets: label and pass condition", UsedIn: []string{
			"* **.targets[].target", "* **.sources[].target", "/v1/targets targets[].sources[]", "* **.tape[].target", "* **.latencyTarget",
			"* **.targets[].group", "/v1/meta targets[].id", "/v1/targets targets[].id", "/v1/targets networks[].cells.<key>",
			"/v1/home insights.valueAll.targets[]", "/v1/home insights.valueAll.rows[].missingTargets[]",
			"/v1/home insights.valueAll.rows[].byTarget.<key>", "/v1/home insights.hardest.id"}})
	RegisterCodeSet(CodeSet{Name: "product_kind", Source: CodesFromCatalog, Catalog: scopeCodes,
		From: "site.scopes: label and summary", UsedIn: []string{
			"* **.scopes[].id", "* **.measuredProduct", "* **.products[].kind", "* **.communityRuns[].product",
			"/v1/dataset providers[].productMeasurements.<id>.product", "/v1/networks/{slug}/runs rows[].product"}})
	RegisterCodeSet(CodeSet{Name: "ranking_profile", Source: CodesFromCatalog, Catalog: profileCodes,
		From:   "site.ranking_profiles: label and description",
		UsedIn: []string{"/v1/meta profiles[].id", "/v1/rankings profile", "/v1/rankings profiles[].id"}})
	RegisterCodeSet(CodeSet{Name: "target_category", Source: CodesFromAPI,
		UsedIn: []string{"* **.targets[].category"},
		Values: codes("baseline", "Baseline: a plain page any proxy should load.",
			"ecommerce", "E-commerce.",
			"search", "Search.",
			"social", "Social.",
			"anti_bot", "Anti-bot: a page behind a bot challenge.",
			"ai_answers", "AI answers.")})
	RegisterCodeSet(CodeSet{Name: "engine", Source: CodesFromAPI, UsedIn: []string{"* **.targets[].engine", "/v1/targets engine",
		"/v1/targets networks[].cells.<id>.engine", "/v1/targets networks[].cells.<id>.otherEngines[]", "* **.browserOverall.<key>",
		"* **.engines.<key>", "/v1/home insights.hardest.engine"},
		Values: codes(model.EngineHTTP, "Plain HTTP requests, no browser.",
			"chrome", "A Chrome browser.",
			"camoufox", "A Camoufox (Firefox) browser.",
			model.EngineBrowser, "Any browser engine.")})
	RegisterCodeSet(CodeSet{Name: "outcome", Source: CodesFromAPI, UsedIn: []string{"* **.outcomes.<key>"},
		Values: codes("challenge", "A challenge page (a captcha) stood in the way.",
			"block", "The site blocked the request.",
			"loginWall", "The site asked to log in.",
			"timeout", "The request took too long.",
			"proxyError", "The proxy failed the request.",
			"noContent", "The page loaded without the expected content.",
			"noAnswer", "The AI answer page gave no answer.",
			"otherError", "Any other error.")})
	RegisterCodeSet(CodeSet{Name: "geo_coverage", Source: CodesFromAPI, UsedIn: []string{"* **.scopes[].geoCoverage.code"},
		Values: []Code{withParams(geoAllUsable, "Every country the network can serve."),
			withParams(geoInventory, "The countries the provider lists in its inventory."),
			withParams(geoRestricted, "A fixed set of {countries} countries.", param("countries", "count", ""))}})
}

// targetCategoryCodes maps the catalog's site.targets.category, its codes
// (api/scripts/catalog-targets.sql) or the English names an older catalog
// row holds, to the target_category code.
var targetCategoryCodes = map[string]string{
	"baseline": "baseline", "ecommerce": "ecommerce", "search": "search", "social": "social", "anti_bot": "anti_bot", "ai_answers": "ai_answers",
	"Baseline": "baseline", "E-commerce": "ecommerce", "Search": "search", "Social": "social", "Anti-bot": "anti_bot", "AI answers": "ai_answers",
}

// targetCategory is a catalog category as its target_category code; null
// (the catalog reason) when the catalog has none or one the set lacks.
func targetCategory(category *string) model.Str {
	if category == nil {
		return nil
	}
	if c, ok := targetCategoryCodes[*category]; ok {
		return model.S(c)
	}
	return nil
}

// targetCodes is the build's targets, each with its catalog label and pass
// condition.
func targetCodes(c *Context) []Code {
	out := []Code{}
	for _, t := range c.Dataset.Targets {
		out = append(out, withParams(t.ID, sentence(model.SV(t.Label), model.SV(t.PassCondition))))
	}
	return out
}

// scopeCodes is the build's test scopes (site.scopes), each with its label
// and summary.
func scopeCodes(c *Context) []Code {
	out := []Code{}
	for _, s := range c.Dataset.Scopes {
		out = append(out, withParams(s.ID, sentence(model.SV(s.Label), model.SV(s.Summary))))
	}
	return out
}

// profileCodes is the build's ranking profiles (site.ranking_profiles), each
// with its label and description, and the rankings' own profile
// (RankingProfile) when the catalog has no row for it.
func profileCodes(c *Context) []Code {
	descriptions, out := map[string]string{}, []Code{}
	if c.Inputs != nil {
		for _, r := range c.Inputs.Profiles {
			descriptions[r.ID] = model.SV(r.Description)
		}
	}
	own := false
	for _, p := range c.Dataset.Profiles {
		own = own || p.ID == RankingProfile
		out = append(out, withParams(p.ID, sentence(model.SV(p.Label), descriptions[p.ID])))
	}
	if !own {
		out = append(out, withParams(RankingProfile, "The rankings' profile: every component at its methodology weight."))
	}
	return out
}

// sentence is a catalog row's label and its text, joined.
func sentence(label, text string) string {
	switch {
	case label == "" && text == "":
		return "No reference text in the catalog."
	case label == "" || text == "":
		return label + text
	}
	return label + ": " + text
}
