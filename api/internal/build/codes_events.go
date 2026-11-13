package build

// The tape and the events: groups, event codes with the values keys each
// fills (eventValues), and severities (CODES-CATALOG-1).

func init() {
	RegisterCodeSet(CodeSet{Name: "event_group", Source: CodesFromAPI, UsedIn: []string{"* **.tape[].kind", "/v1/events events[].category",
		"/v1/events/stats totals.byCategory.<key>", "/v1/events/stats buckets[].byCategory.<key>", "/v1/events/stats heatmap[].byCategory.<key>"},
		Values: codes("census", "The daily tests, censuses and hourly figures.",
			"pulse", "The pulse tests.",
			"incident", "Outages and sudden changes.",
			"audit", "Pool audits: shared addresses and new hosting networks.")})
	RegisterCodeSet(CodeSet{Name: "event", Source: CodesFromAPI, Values: eventCodes(),
		UsedIn: []string{"* **.tape[].event", "/v1/events events[].event", "/v1/events events[].kind"}})
	RegisterCodeSet(CodeSet{Name: "event_severity", Source: CodesFromAPI, UsedIn: []string{"* **.tape[].severity", "/v1/events events[].severity"},
		Values: codes("info", "Routine.", "minor", "Worth a look.", "major", "Serious.", "critical", "The network is down.")})
}

func eventCodes() []Code {
	requests, successes, p50 := param("requests", "count", ""), param("successes", "count", ""), param("p50", "ms", "")
	ips, countries, duration := param("uniqueIps", "count", ""), param("countries", "count", ""), param("durationSeconds", "seconds", "")
	share, ipsValue := param("value", "ratio", ""), param("value", "count", "")
	rate := []CodeParam{requests, successes, p50, param("baselineRate", "ratio", ""), share}
	hour := []CodeParam{param("hour", "timestamp", ""), ips, countries, ipsValue}
	return []Code{
		withParams("daily_test_started", "A daily test started in {countries} countries.", countries, param("value", "count", "")),
		withParams("daily_test_finished", "A daily test finished: {requests} requests, {uniqueIps} addresses in {durationSeconds}.",
			countries, requests, ips, duration, ipsValue),
		withParams("census_started", "A census started."),
		withParams("census_finished", "A census finished: {requests} requests, {uniqueIps} addresses in {durationSeconds}.",
			requests, ips, duration, ipsValue),
		withParams("census_retry", "A census is tried again."),
		withParams("census_skipped", "A census was skipped."),
		withParams("hour_released", "An hour's figures were released: {uniqueIps} addresses in {countries} countries.", hour...),
		withParams("pulse_finished", "A pulse test finished: {successes} of {requests} succeeded.", requests, successes, p50,
			param("challenges", "count", ""), param("blocks", "count", ""), param("previousP50", "ms", ""),
			param("previousSuccesses", "count", ""), param("previousRequests", "count", ""), share),
		withParams("pulse_down", "A pulse failed: {errors} of {requests} requests in {windowMinutes} over {regions} regions.",
			requests, param("errors", "count", ""), param("windowMinutes", "minutes", ""), param("regions", "count", "")),
		withParams("pulse_recovered", "Pulses succeed again after {durationSeconds}.", duration),
		withParams("down", "The network is down: no success against a baseline of {baselineRate}.", rate...),
		withParams("recovered", "The network recovered after {durationSeconds}.", append(append([]CodeParam{}, rate...), duration)...),
		withParams("success_drop", "Success fell to {value} against a baseline of {baselineRate}.", rate...),
		withParams("latency_spike", "Latency spiked to {p50} against a baseline of {baseline}.", requests, successes, p50,
			param("baseline", "ms", ""), param("value", "ms", "")),
		withParams("latency_rise", "Latency rose to {p50} against a baseline of {baseline}.", p50, param("baseline", "ms", ""),
			param("value", "ms", "")),
		withParams("block_spike", "Blocks rose to {blockShare} against a baseline of {baselineBlockShare}.", requests, successes, p50,
			param("blockShare", "ratio", ""), param("baselineBlockShare", "ratio", ""), share),
		withParams("ip_overlap", "{sharedIps} of {uniqueIps} addresses are shared with {peers} networks.",
			param("share", "ratio", ""), param("sharedIps", "count", ""), ips, param("peers", "count", ""), param("runs", "count", ""), share,
			param("hour", "timestamp", "")),
		withParams("new_hosting_asn", "A new hosting network {asn} appeared among the exits.", param("asn", "integer", ""), ipsValue),
		withParams("node_offline", "A test node went offline."),
		withParams("no_node", "No test node was available."),
	}
}
