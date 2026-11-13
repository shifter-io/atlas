package build

// The changelog: entry kinds, title and detail keys, severities, units and
// the incident block's codes (CODES-CATALOG-1).

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
	v1 := func(typ, unit string) []CodeParam { return []CodeParam{param("v1", typ, unit)} }
	RegisterCodeSet(CodeSet{Name: "change_title", Source: CodesFromAPI, UsedIn: entry("title.key"), Values: []Code{
		withParams("success_rose", "Success rose by {v1} points.", v1("number", "points")...),
		withParams("success_fell", "Success fell by {v1} points.", v1("number", "points")...),
		withParams("latency_rose", "Latency rose by {v1}.", v1("number", "percent")...),
		withParams("latency_fell", "Latency fell by {v1}.", v1("number", "percent")...),
		withParams("pool_grew", "The pool grew by {v1}.", v1("number", "percent")...),
		withParams("pool_shrank", "The pool shrank by {v1}.", v1("number", "percent")...),
		withParams("countries_lost", "{v1} countries lost.", v1("count", "")...),
		withParams("geo_accuracy_rose", "Location accuracy rose by {v1} points.", v1("number", "points")...),
		withParams("geo_accuracy_fell", "Location accuracy fell by {v1} points.", v1("number", "points")...),
		withParams("price_raised", "The list price rose by {v1}.", v1("number", "percent")...),
		withParams("price_cut", "The list price fell by {v1}.", v1("number", "percent")...)}})
	RegisterCodeSet(CodeSet{Name: "change_detail", Source: CodesFromAPI, UsedIn: entry("detail.key"), Values: []Code{
		withParams("success_now", "Success is now {v1}.", v1("number", "percent")...),
		withParams("latency_now", "Latency is now {v1}.", v1("ms", "")...),
		withParams("pool_unique_ips", "{v1} unique addresses.", v1("count", "")...),
		withParams("geo_accuracy_now", "Location accuracy is now {v1}.", v1("number", "percent")...),
		withParams(notSeenIn["24h"], "Not seen in the last 24 hours: {v1}.", v1("country_list", "")...),
		withParams(notSeenIn["7d"], "Not seen in the last 7 days: {v1}.", v1("country_list", "")...),
		withParams("price_changed", "From {v1} to {v2} per GB.", param("v1", "usd_per_gb", ""), param("v2", "usd_per_gb", ""))}})
	RegisterCodeSet(CodeSet{Name: "change_severity", Source: CodesFromAPI, UsedIn: entry("severity"),
		Values: codes("info", "Neither better nor worse.", "good", "A change for the better.", "bad", "A change for the worse.")})
	RegisterCodeSet(CodeSet{Name: "change_unit", Source: CodesFromAPI, UsedIn: entry("unit"),
		Values: codes("pts", "Percentage points.", "%", "Percent.", "countries", "Countries.")})
	RegisterCodeSet(CodeSet{Name: "change_metric", Source: CodesFromAPI, UsedIn: entry("values.metric"),
		Values: codes("success", "The success rate.", "latency", "The latency.")})
	RegisterCodeSet(CodeSet{Name: "incident_severity", Source: CodesFromAPI, UsedIn: entry("incident.severity"),
		Values: codes("minor", "Short and narrow.", "moderate", "Between minor and major.", "major", "Long or wide.")})
	RegisterCodeSet(CodeSet{Name: "incident_kind", Source: CodesFromAPI, UsedIn: append(entry("incident.kind"), "* **.incidents[].kind"),
		Values: codes("down", "The network failed its tests.", "census_stall", "A census stalled.")})
	RegisterCodeSet(CodeSet{Name: "incident_detected_by", Source: CodesFromAPI,
		UsedIn: append(entry("incident.detectedBy"), "* **.incidents[].detectedBy"),
		Values: codes("pulse", "The pulse tests.", "census", "A census.")})
}
