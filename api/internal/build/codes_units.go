package build

import "github.com/Zeus-Development/proxy-index/api/internal/model"

// The units, the ranges, the kinds and the sources (CODES-CATALOG-1).

func init() {
	RegisterCodeSet(CodeSet{Name: "unit", Source: CodesFromAPI, UsedIn: []string{"/v1/codes sets[].values[].params[].unit"},
		Values: codes(
			"ratio", "A share from 0 to 1.",
			"percent", "A percent from 0 to 100.",
			"points", "Percentage points: the difference of two percents.",
			"ms", "Milliseconds.",
			"seconds", "Seconds.",
			"minutes", "Minutes.",
			"days", "Days.",
			"usd", "US dollars.",
			"usd_per_gb", "US dollars per gigabyte.",
			"gb", "Gigabytes.",
			"mb", "Megabytes.",
			"kb", "Kilobytes.",
			"count", "A whole number of things.",
			"score_1_10", "A score from 1 to 10.",
			"score_0_100", "A score from 0 to 100.")})
	ranges := make([]Code, 0, len(Ranges))
	for _, r := range Ranges {
		ranges = append(ranges, withParams(r.ID, rangeSentence[r.ID]))
	}
	RegisterCodeSet(CodeSet{Name: "range", Source: CodesFromAPI, Values: ranges, UsedIn: []string{"* range", "* **.range.id",
		"* **.range.short", "/v1/meta ranges[].id", "/v1/meta ranges[].short", "/v1/networks/{slug} ranges.<key>",
		"/v1/networks/{slug} stats.<key>", "/v1/countries/{cc} ranges.<key>"}})
	kinds := make([]Code, 0, len(model.ProxyKinds))
	for _, k := range model.ProxyKinds {
		kinds = append(kinds, withParams(k, kindSentence[k]))
	}
	RegisterCodeSet(CodeSet{Name: "proxy_kind", Source: CodesFromAPI, Values: kinds, UsedIn: []string{"* **.kinds[]",
		"* **.primaryKind", "* **.networks[].kind", "* **.tape[].product", "* **.framework.products[]",
		"/v1/dataset providers[].productMeasurements.<key>", "/v1/dataset providers[].productMeasurements.<id>.kind", "/v1/targets kind", "/v1/overlap kind",
		"/v1/networks/{slug}/runs kind", "/v1/countries/{cc} ranges.<id>[].kind"}})
	RegisterCodeSet(CodeSet{Name: "proxy_kind_or_all", Source: CodesFromAPI,
		UsedIn: []string{"/v1/networks/{slug} kind", "/v1/networks/{slug} network.kind", "/v1/networks/{slug}/chart kind"},
		Values: append(append([]Code{}, kinds...), withParams(AllNetworks, "Every kind together: the all-networks answer."))})
	RegisterCodeSet(CodeSet{Name: "source", Source: CodesFromAPI, UsedIn: []string{"* **.tape[].source", "* **.targets[].source",
		"* **.networks[].source", "* **.communityRuns[].source", "* **.providers[].source", "* **.provider.source", "* network.source",
		"* profile.source", "/v1/changelog entries[].source", "* **.changelog[].source", "/v1/coverage/{slug} source", "/v1/networks/{slug}/runs rows[].source"},
		Values: codes(model.SourceMeasured, "Measured by the Proxy Index tests.",
			"catalog", "From the catalog only: not measured.",
			model.SourceEditorial, "Written by hand by the editors.")})
}

var rangeSentence = map[string]string{"24h": "The last 24 hours.", "7d": "The last 7 days.", "30d": "The last 30 days."}

var kindSentence = map[string]string{"residential": "Residential proxies: home connections.",
	"datacenter": "Datacenter proxies: server addresses.", "mobile": "Mobile proxies: cellular connections.",
	"isp": "ISP proxies: static addresses registered to a consumer network."}
