package build

import (
	"encoding/json"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// valuesCases are every event kind with the raw data the framework writes and
// the values it reads as (JSON keys sorted): the 8 keys of every row (6 on a
// success kind: no value or baseline, #1072), the kind's own keys and value,
// its headline.
var valuesCases = []struct{ name, kind, data, want string }{
	{"pulse", "pulse_finished",
		`{"requests":100,"successes":49,"success_rate":0.49,"p50":527.4,"challenges":1,"blocks":0,"value":0.49,"previous":{"success_rate":0.9,"successes":90,"requests":100,"p50":401,"challenges":0,"blocks":0}}`,
		`{"blocks":0,"challenges":1,"hour":null,"p50":527.4,"previousP50":401,"previousRequests":100,"previousSuccesses":90,"requests":100,"sharedIps":null,"successes":49,"uniqueIps":null}`},
	{"pulse stored before the previous counts", "pulse_finished",
		`{"requests":100,"successes":49,"p50":527.4,"value":0.49,"previous":{"success_rate":0.9,"p50":401,"challenges":0,"blocks":0}}`,
		`{"blocks":null,"challenges":null,"hour":null,"p50":527.4,"previousP50":401,"previousRequests":null,"previousSuccesses":null,"requests":100,"sharedIps":null,"successes":49,"uniqueIps":null}`},
	{"pulse, no previous", "pulse_finished", `{"requests":10,"p50":null}`,
		`{"blocks":null,"challenges":null,"hour":null,"p50":null,"previousP50":null,"previousRequests":null,"previousSuccesses":null,"requests":10,"sharedIps":null,"successes":null,"uniqueIps":null}`},
	{"latency spike", "latency_spike", `{"requests":100,"successes":98,"p50":1190,"baseline":400,"value":1190}`,
		`{"baseline":400,"hour":null,"p50":1190,"requests":100,"sharedIps":null,"successes":98,"uniqueIps":null,"value":1190}`},
	{"latency spike, value only", "latency_spike", `{"value":900,"baseline":300}`,
		`{"baseline":300,"hour":null,"p50":900,"requests":null,"sharedIps":null,"successes":null,"uniqueIps":null,"value":900}`},
	{"down", "down", `{"requests":100,"successes":0,"p50":null,"baseline":0.95,"value":0,"source":"pulse"}`,
		`{"hour":null,"p50":null,"requests":100,"sharedIps":null,"successes":0,"uniqueIps":null}`},
	{"success drop", "success_drop", `{"requests":100,"successes":40,"p50":600,"baseline":0.9,"value":0.4}`,
		`{"hour":null,"p50":600,"requests":100,"sharedIps":null,"successes":40,"uniqueIps":null}`},
	{"recovered after a census outage", "recovered", `{"requests":50,"successes":50,"success_rate":1,"p50":300,"source":"census","duration_s":120}`,
		`{"durationSeconds":120,"hour":null,"p50":300,"requests":50,"sharedIps":null,"successes":50,"uniqueIps":null}`},
	{"recovered after a pulse incident", "recovered", `{"requests":50,"successes":49,"p50":300,"baseline":0.9,"value":0.98,"incident":"down"}`,
		`{"durationSeconds":null,"hour":null,"p50":300,"requests":50,"sharedIps":null,"successes":49,"uniqueIps":null}`},
	{"block spike", "block_spike", `{"requests":100,"successes":70,"p50":500,"baseline":0.02,"value":0.3}`,
		`{"baseline":null,"baselineBlockShare":0.02,"blockShare":0.3,"hour":null,"p50":500,"requests":100,"sharedIps":null,"successes":70,"uniqueIps":null,"value":0.3}`},
	{"hour released", "estimate_hour_released", `{"day":"2026-11-02","hour":7,"formula_version":"v3","unique_ips":154770,"countries":12}`,
		`{"baseline":null,"countries":12,"hour":"2026-11-02T07:00:00.000Z","p50":null,"requests":null,"sharedIps":null,"successes":null,"uniqueIps":154770,"value":154770}`},
	{"hour not whole", "estimate_hour_released", `{"day":"2026-11-02","hour":7.5}`,
		`{"baseline":null,"countries":null,"hour":null,"p50":null,"requests":null,"sharedIps":null,"successes":null,"uniqueIps":null,"value":null}`},
	{"hour without a day", "estimate_hour_released", `{"hour":7}`,
		`{"baseline":null,"countries":null,"hour":null,"p50":null,"requests":null,"sharedIps":null,"successes":null,"uniqueIps":null,"value":null}`},
	{"daily started", "daily_test_started", `{"region":"fra1","countries":["DE","FR"]}`, `{"baseline":null,"countries":2,"hour":null,"p50":null,"requests":null,"sharedIps":null,"successes":null,"uniqueIps":null,"value":2}`},
	{"daily finished", "daily_test_finished", `{"countries":["DE"],"status":"done","requests":1234,"unique_ips":900,"duration_s":5400}`,
		`{"baseline":null,"countries":1,"durationSeconds":5400,"hour":null,"p50":null,"requests":1234,"sharedIps":null,"successes":null,"uniqueIps":900,"value":900}`},
	{"daily failed, no counters", "daily_test_finished", `{"countries":"DE","status":"failed"}`,
		`{"baseline":null,"countries":null,"durationSeconds":null,"hour":null,"p50":null,"requests":null,"sharedIps":null,"successes":null,"uniqueIps":null,"value":null}`},
	{"overlap in a census slot", "ip_overlap", `{"value":0.25,"shared_ips":5,"unique_ips":20,"peers":{"a":3,"b":2}}`,
		`{"baseline":null,"hour":null,"p50":null,"peers":2,"requests":null,"runs":null,"share":0.25,"sharedIps":5,"successes":null,"uniqueIps":20,"value":0.25}`},
	{"overlap in an hour", "ip_overlap", `{"value":0.5,"shared_ips":1,"unique_ips":2,"peers":{},"hour":"2026-11-02T07:00:00+00:00","runs":3}`,
		`{"baseline":null,"hour":"2026-11-02T07:00:00.000Z","p50":null,"peers":0,"requests":null,"runs":3,"share":0.5,"sharedIps":1,"successes":null,"uniqueIps":2,"value":0.5}`},
	{"overlap, hour not a time", "ip_overlap", `{"value":0.5,"hour":"late"}`,
		`{"baseline":null,"hour":null,"p50":null,"peers":null,"requests":null,"runs":null,"share":0.5,"sharedIps":null,"successes":null,"uniqueIps":null,"value":0.5}`},
	{"hosting asn", "new_hosting_asn", `{"region":"fra1","asn":16509,"name":"Example Hosting","usage_type":"DCH"}`, `{"asn":16509,"baseline":null,"hour":null,"p50":null,"requests":null,"sharedIps":null,"successes":null,"uniqueIps":null,"value":null}`},
	{"pulse down", "pulse_down", `{"errors":3,"requests":10,"window_minutes":15,"regions":1,"threshold":0.5}`,
		`{"baseline":null,"errors":3,"hour":null,"p50":null,"regions":1,"requests":10,"sharedIps":null,"successes":null,"uniqueIps":null,"value":null,"windowMinutes":15}`},
	{"pulse recovered", "pulse_recovered", `{"duration_s":120.5}`, `{"baseline":null,"durationSeconds":120.5,"hour":null,"p50":null,"requests":null,"sharedIps":null,"successes":null,"uniqueIps":null,"value":null}`},
	{"latency rise", "latency_rise", `{"p50":1500,"baseline":400,"baseline_source":"daily"}`, `{"baseline":400,"hour":null,"p50":1500,"requests":null,"sharedIps":null,"successes":null,"uniqueIps":null,"value":1500}`},
	{"census done", "census_finished", `{"requests":2000,"unique_ips":12,"duration_s":30}`, `{"baseline":null,"durationSeconds":30,"hour":null,"p50":null,"requests":2000,"sharedIps":null,"successes":null,"uniqueIps":12,"value":12}`},
	{"census started", "census_started", `{}`, `{"baseline":null,"hour":null,"p50":null,"requests":null,"sharedIps":null,"successes":null,"uniqueIps":null,"value":null}`},
	{"census rerun", "census_retry", `{"reason":"no_success"}`, `{"baseline":null,"hour":null,"p50":null,"requests":null,"sharedIps":null,"successes":null,"uniqueIps":null,"value":null}`},
	{"census skipped", "census_skipped", `{"reason":"busy"}`, `{"baseline":null,"hour":null,"p50":null,"requests":null,"sharedIps":null,"successes":null,"uniqueIps":null,"value":null}`},
	{"node offline", "node_offline", `{}`, `{"baseline":null,"hour":null,"p50":null,"requests":null,"sharedIps":null,"successes":null,"uniqueIps":null,"value":null}`},
	{"no node", "no_node", `{}`, `{"baseline":null,"hour":null,"p50":null,"requests":null,"sharedIps":null,"successes":null,"uniqueIps":null,"value":null}`},
	{"an unknown kind", "something_new", `{"value":1}`, `{"baseline":null,"hour":null,"p50":null,"requests":null,"sharedIps":null,"successes":null,"uniqueIps":null,"value":null}`},
	{"not finite", "latency_rise", `{"p50":"fast","baseline":true}`, `{"baseline":null,"hour":null,"p50":null,"requests":null,"sharedIps":null,"successes":null,"uniqueIps":null,"value":null}`},
}

func TestEventValuesOfEveryKind(t *testing.T) {
	for _, c := range valuesCases {
		t.Run(c.name, func(t *testing.T) {
			var data map[string]any
			if err := json.Unmarshal([]byte(c.data), &data); err != nil {
				t.Fatal(err)
			}
			got, err := json.Marshal(eventValues(c.kind, data))
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != c.want {
				t.Errorf("values = %s, want %s", got, c.want)
			}
		})
	}
}

// A tape row carries its event's country, region, target, severity and
// values, and the same event's /v1/events row the same values, event code
// and kind, the group (#993).
func TestTapeAndEventRowsCarryTheSameValues(t *testing.T) {
	rows := []store.Event{}
	for i, c := range valuesCases {
		rows = append(rows, store.Event{ID: int64(len(valuesCases) - i), At: windowBase, Kind: c.kind, Provider: sp("acme"),
			Product: sp("residential"), Country: sp("DE"), Region: sp("fra1"), Target: sp("t1"), Severity: "minor",
			Data: json.RawMessage(c.data)})
	}
	js := make([]store.EventJSON, len(rows))
	for i := range rows {
		js[i] = rows[i].JSON()
	}
	tape := TapeFromEvents(js, acmeText.products, "", acmeText.slugs)
	events := acmeText.render(rows)
	for i, c := range valuesCases {
		r := tape[i]
		if model.SV(r.Country) != "DE" || model.SV(r.Region) != "fra1" || model.SV(r.Target) != "t1" || model.SV(r.Severity) != "minor" {
			t.Errorf("%s: tape row %+v", c.name, r)
		}
		got, _ := json.Marshal(r.Values)
		event, _ := json.Marshal(events[i].Values)
		if string(got) != c.want || string(event) != c.want {
			t.Errorf("%s: tape values %s, event values %s, want %s", c.name, got, event, c.want)
		}
		if events[i].Event != r.Event || events[i].Kind != r.Kind || r.Kind != store.Category(c.kind) {
			t.Errorf("%s: event row event %q kind %q, tape row event %q kind %q", c.name, events[i].Event, events[i].Kind, r.Event, r.Kind)
		}
	}
}

// The new fields' nulls are the contract's: a tape row's na does not list
// them.
func TestTapeNAOmitsTheEventsNulls(t *testing.T) {
	row := TapeFromEvents([]store.EventJSON{{ID: 1, Kind: "pulse_finished", Category: "pulse", Data: json.RawMessage(`{}`)}}, nil, "", nil)[0]
	na, err := naOf(row, nil, false, tapeContractNull...)
	if err != nil || len(na) != 0 {
		t.Fatalf("na %v, err %v", na, err)
	}
	editorial := model.TapeEvent{Values: eventValues("", nil)}
	if na, _ = naOf(editorial, nil, false, tapeContractNull...); len(na) != 0 {
		t.Fatalf("editorial na %v", na)
	}
}
