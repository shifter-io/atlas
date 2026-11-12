package testscale

import (
	"encoding/json"
	"fmt"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// tapeKinds are a full home tape's rows (HOME-TAPE-1: each tab's newest 10),
// each kind with the raw data its producer writes (detect.py,
// orchestrator/events.py, daily/audits.py) at full width.
var tapeKinds = []struct {
	kind, severity string
	n              int
	data           string
}{
	{"estimate_hour_released", "info", 6, `{"day":"2026-11-02","hour":%d,"formula_version":"hour-v3","unique_ips":154770,"countries":41}`},
	{"daily_test_started", "info", 2, `{"region":"fra1","countries":["DE","FR","NL","GB","PL","%02d"]}`},
	{"daily_test_finished", "info", 2, `{"region":"fra1","countries":["DE","FR","NL","GB","PL","%02d"],"status":"done","requests":120000,"unique_ips":48213,"duration_s":3412.5}`},
	{"pulse_finished", "info", 10, `{"region":"fra1","batch_finished_at":"2026-11-03T03:5%d:00+00:00","requests":100,"successes":49,"success_rate":0.49,"p50":527.25,"challenges":0,"blocks":3,"value":0.49,"previous":{"success_rate":0.97,"p50":412.5,"challenges":0,"blocks":0}}`},
	{"latency_spike", "minor", 3, `{"region":"fra1","batch_finished_at":"2026-11-03T03:5%d:00+00:00","requests":100,"successes":97,"success_rate":0.97,"p50":1190.5,"challenges":0,"blocks":0,"baseline":402.25,"value":1190.5}`},
	{"down", "critical", 2, `{"region":"fra1","batch_finished_at":"2026-11-03T03:5%d:00+00:00","requests":100,"successes":0,"success_rate":0,"p50":null,"challenges":0,"blocks":0,"baseline":0.9725,"value":0,"since":"2026-11-03T03:40:00+00:00","source":"pulse"}`},
	{"success_drop", "major", 2, `{"region":"fra1","batch_finished_at":"2026-11-03T03:5%d:00+00:00","requests":100,"successes":41,"success_rate":0.41,"p50":733.5,"challenges":2,"blocks":9,"baseline":0.9675,"value":0.41}`},
	{"recovered", "info", 2, `{"region":"fra1","batch_finished_at":"2026-11-03T03:5%d:00+00:00","requests":100,"successes":96,"success_rate":0.96,"p50":455.75,"challenges":0,"blocks":0,"baseline":0.9725,"value":0.96,"incident":"down"}`},
	{"block_spike", "major", 1, `{"region":"fra1","batch_finished_at":"2026-11-03T03:5%d:00+00:00","requests":100,"successes":64,"success_rate":0.64,"p50":610.25,"challenges":4,"blocks":31,"baseline":0.0125,"value":0.35}`},
	{"ip_overlap", "info", 7, `{"region":"fra1","value":0.0412,"shared_ips":187,"unique_ips":4539,"peers":{"net01":120,"net02":51,"net%02d":16},"hour":"2026-11-03T03:00:00+00:00","runs":6}`},
	{"new_hosting_asn", "minor", 3, `{"region":"fra1","asn":2096%d,"name":"Example Hosting Networks GmbH","usage_type":"DCH"}`},
}

// TapeEvents is a full home tape of the real shape: 40 events, 10 per tab,
// newest first a minute apart from 2026-11-03 04:00 UTC, spread over the
// networks, each with a country, a region and (the pulse-judged kinds) a
// target, as test_events holds them. No two merge (EVENTS-MERGE-1).
func TapeEvents(networks int) []store.Event {
	at := time.Date(2026, 11, 3, 4, 0, 0, 0, time.UTC)
	out := []store.Event{}
	for _, k := range tapeKinds {
		for j := range k.n {
			i := len(out)
			provider, product, region := fmt.Sprintf("net%02d", i%max(networks, 1)), "residential", "fra1"
			country := countryCode(i % 40)
			var target *string
			if k.severity != "info" || k.kind == "pulse_finished" || k.kind == "recovered" {
				t := []string{"generic_http", "generic_https", "google_search"}[j%3]
				target = &t
			}
			out = append(out, store.Event{ID: int64(100000 - i), At: at.Add(-time.Duration(i) * time.Minute), Kind: k.kind,
				Provider: &provider, Product: &product, Country: &country, Region: &region, Target: target,
				Signal: -1, Severity: k.severity, Data: json.RawMessage(fmt.Sprintf(k.data, j))})
		}
	}
	return out
}
