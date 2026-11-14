package build

// The changelog: entry kinds, events with the values keys each fills,
// severities and the incident block's codes (CODES-CATALOG-1,
// CHANGELOG-EVENT-1; the title, detail and unit codes went with those
// fields, CHANGELOG-TEXT-REMOVE-1).

func init() {
	entry := func(path ...string) []string {
		out := []string{}
		for _, p := range path {
			out = append(out, "/v1/changelog entries[]."+p, "/v1/networks/{slug} changelog[]."+p)
		}
		return out
	}
	RegisterCodeSet(CodeSet{Name: "change_kind", Source: CodesFromAPI, UsedIn: entry("kind"),
		Values: codes("incident", "An outage.", "price", "A list price changed.", "pool", "The pool grew or shrank.",
			"performance", "Success or latency moved.", "authenticity", "Location accuracy moved.",
			"coverage", "Countries were lost.", "methodology", "The methodology changed.")})
	RegisterCodeSet(CodeSet{Name: "change_event", Source: CodesFromAPI, UsedIn: entry("event"), Values: changeEventCodes()})
	RegisterCodeSet(CodeSet{Name: "change_severity", Source: CodesFromAPI, UsedIn: entry("severity"),
		Values: codes("info", "Neither better nor worse.", "good", "A change for the better.", "bad", "A change for the worse.")})
	RegisterCodeSet(CodeSet{Name: "change_metric", Source: CodesFromAPI, UsedIn: entry("values.metric"),
		Values: codes("success", "The success rate.", "latency", "The latency.")})
	RegisterCodeSet(CodeSet{Name: "incident_severity", Source: CodesFromAPI, UsedIn: append(entry("incident.severity"), "* **.incidents[].severity", "* **.bands[].severity"),
		Values: codes("minor", "Short and narrow.", "moderate", "Between minor and major.", "major", "Long or wide.")})
	RegisterCodeSet(CodeSet{Name: "incident_kind", Source: CodesFromAPI, UsedIn: append(entry("incident.kind"), "* **.incidents[].kind"),
		Values: codes("down", "The network failed its tests.", "census_stall", "A census stalled.")})
	RegisterCodeSet(CodeSet{Name: "incident_detected_by", Source: CodesFromAPI,
		UsedIn: append(entry("incident.detectedBy"), "* **.incidents[].detectedBy"),
		Values: codes("pulse", "The pulse tests.", "census", "A census.")})
}

// changeEventCodes is every changelog event and the values keys it fills
// (changeValueKeys): a delta is signed, its sentence words its size.
func changeEventCodes() []Code {
	pts, pct := param("delta", "number", "points"), param("deltaPct", "number", "percent")
	success := []CodeParam{param("metric", "code:change_metric", ""), param("value", "ratio", ""), param("previous", "ratio", ""), pts}
	latency := []CodeParam{param("metric", "code:change_metric", ""), param("value", "ms", ""), param("previous", "ms", ""),
		param("delta", "number", "percent")}
	pool := []CodeParam{param("uniqueIps", "count", ""), param("previousUniqueIps", "count", ""), pct}
	geo := []CodeParam{param("value", "ratio", ""), param("previous", "ratio", ""), pts}
	price := []CodeParam{param("fromRate", "usd_per_gb", ""), param("toRate", "usd_per_gb", ""), pct, param("plan", "name", "")}
	return []Code{
		withParams("success_rose", "Success rose by {delta} points.", success...),
		withParams("success_fell", "Success fell by {delta} points.", success...),
		withParams("latency_rose", "Latency rose by {delta}.", latency...),
		withParams("latency_fell", "Latency fell by {delta}.", latency...),
		withParams("pool_grew", "The pool grew by {deltaPct}.", pool...),
		withParams("pool_shrank", "The pool shrank by {deltaPct}.", pool...),
		withParams("countries_lost", "{delta} countries lost: not seen in the last {comparedRange}: {lostCountryCodes}.",
			param("countries", "count", ""), param("previousCountries", "count", ""), param("delta", "integer", "count"),
			param("lostCountryCodes", "country_list", ""), param("comparedRange", "code:range", "")),
		withParams("geo_accuracy_rose", "Location accuracy rose by {delta} points.", geo...),
		withParams("geo_accuracy_fell", "Location accuracy fell by {delta} points.", geo...),
		withParams("price_raised", "The list price rose by {deltaPct}.", price...),
		withParams("price_cut", "The list price fell by {deltaPct}.", price...),
		withParams("incident", "An outage: its facts are in the entry's incident block."),
		withParams("catalog", "A hand-written entry: its kind's values keys, each null."),
	}
}
