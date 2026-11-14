package build

// The tape and the events: groups, event codes with the values keys each
// fills (eventValues), and severities (CODES-CATALOG-1).

func init() {
	RegisterCodeSet(CodeSet{Name: "event_group", Source: CodesFromAPI, UsedIn: []string{"* **.tape[].kind", "/v1/events events[].kind",
		"/v1/events/stats totals.byCategory.<key>", "/v1/events/stats buckets[].byCategory.<key>", "/v1/events/stats heatmap[].byCategory.<key>"},
		Values: codes("census", "The daily tests, censuses and hourly figures.",
			"pulse", "The pulse tests.",
			"incident", "Outages and sudden changes.",
			"audit", "Pool audits: shared addresses and new hosting networks.")})
	RegisterCodeSet(CodeSet{Name: "event", Source: CodesFromAPI, Values: eventCodes(),
		UsedIn: []string{"* **.tape[].event", "/v1/events events[].event"}})
	RegisterCodeSet(CodeSet{Name: "event_severity", Source: CodesFromAPI, UsedIn: []string{"* **.tape[].severity", "/v1/events events[].severity"},
		Values: codes("info", "Routine.", "minor", "Worth a look.", "major", "Serious.", "critical", "The network is down.")})
}

func eventCodes() []Code {
	requests, successes, p50 := param("requests", "count", ""), param("successes", "count", ""), param("p50", "ms", "")
	ips, countries, duration := param("uniqueIps", "count", ""), param("countries", "count", ""), param("durationSeconds", "seconds", "")
	share, ipsValue := param("value", "ratio", ""), param("value", "count", "")
	rate := []CodeParam{requests, successes, p50} // no success share (#1072)
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
			param("previousSuccesses", "count", ""), param("previousRequests", "count", "")),
		withParams("pulse_down", "A pulse failed: {errors} of {requests} requests in {windowMinutes} over {regions} regions.",
			requests, param("errors", "count", ""), param("windowMinutes", "minutes", ""), param("regions", "count", "")),
		withParams("pulse_recovered", "Pulses succeed again after {durationSeconds}.", duration),
		withParams("down", "The network is down: {successes} of {requests} requests succeeded.", rate...),
		withParams("recovered", "The network recovered after {durationSeconds}.", append(append([]CodeParam{}, rate...), duration)...),
		withParams("success_drop", "Success fell: {successes} of {requests} requests succeeded.", rate...),
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

// The data keys of a pulse-judged event, and of a judgement against a baseline.
var (
	pulseData  = []string{"region", "batch_finished_at", "requests", "successes", "success_rate", "p50", "challenges", "blocks"}
	judgedData = dataKeys(pulseData, "baseline", "value")
)

// eventData is each raw event kind's data keys values may read
// (SEC-EVENTS-SAFE-1): no other key of an event's stored data reaches an
// answer, so a key a producer adds is read only once it is listed here, and a
// kind not listed reads none. A key ending in codeOnly is read only when its
// value is a code: a node may write free text there.
var eventData = map[string][]string{
	"daily_test_started":     {"region", "countries"},
	"daily_test_finished":    {"region", "countries", "status", "requests", "unique_ips", "duration_s"},
	"estimate_hour_released": {"day", "hour", "formula_version", "unique_ips", "countries"},
	"census_started":         {"region", "start_at"},
	"census_finished": {"region", "requests", "successes", "unique_ips", "success_rate", "duration_s", "stop_reason" + codeOnly,
		"status", "bytes_up", "bytes_down"},
	"census_retry": {"region", "retry_of", "reason" + codeOnly, "pulse_batch_id"},
	"census_skipped": {"region", "reason", "previous_batch_id", "scheduled_for", "status", "runs_killed", "requests", "successes",
		"unique_ips"},
	"pulse_finished":  dataKeys(judgedData, "previous", "reason", "status", "runs_killed", "unique_ips"),
	"pulse_down":      {"window_start", "threshold", "outage_ids", "window_minutes", "errors", "requests", "regions"},
	"pulse_recovered": {"down_since", "duration_s", "outage_ids"},
	"down":            dataKeys(judgedData, "since", "source", "batch_id"),
	"recovered":       dataKeys(judgedData, "incident", "source", "since", "duration_s", "outage_batch_id"),
	"success_drop":    judgedData,
	"latency_spike":   judgedData,
	"latency_rise":    {"p50", "baseline", "baseline_source"},
	"block_spike":     judgedData,
	"ip_overlap":      {"region", "value", "shared_ips", "unique_ips", "peers", "hour", "runs"},
	"new_hosting_asn": {"region", "asn", "name", "usage_type"},
	"node_offline":    {"region", "provider", "product", "last_heartbeat"},
	"no_node":         {"region", "provider", "product", "kind", "batches", "countries"},
}

// codeOnly marks an eventData key kept only when its value is a code.
const codeOnly = "=code"

// dataKeys is base and more, in a new list.
func dataKeys(base []string, more ...string) []string {
	return append(append([]string{}, base...), more...)
}
