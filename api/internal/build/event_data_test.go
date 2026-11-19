package build

import (
	"encoding/json"
	"maps"
	"slices"
	"strings"
	"testing"

	"github.com/shifter-io/atlas/api/internal/testscale"
)

// An event's data keeps its registered keys in their stored order, a
// code-only key only as a code, and nothing of a kind not registered.
func TestPublicDataKeepsRegisteredKeys(t *testing.T) {
	t.Parallel()
	for _, c := range []struct{ kind, raw, want string }{
		{"node_offline", `{"region": "fra1", "host": "h", "node_id": 7, "provider": "acme", "last_heartbeat": null}`,
			`{"region":"fra1","provider":"acme","last_heartbeat":null}`},
		{"census_finished", `{"error": "x", "status": "failed", "stop_reason": "stalled"}`, `{"status":"failed","stop_reason":"stalled"}`},
		{"census_retry", `{"reason": "run failed: see the log", "retry_of": 3}`, `{"retry_of":3}`},
		{"census_retry", `{"reason": "no_success"}`, `{"reason":"no_success"}`},
		{"pulse_finished", `{"value": 0.5, "previous": {"p50": 1, "requests": 2}, "extra": [1]}`,
			`{"value":0.5,"previous":{"p50": 1, "requests": 2}}`},
		{"future_kind", `{"region": "fra1"}`, `{}`},
		{"down", `null`, `{}`},
		{"down", `[1]`, `{}`},
		{"down", `{"value": }`, `{}`},
	} {
		if got := string(publicData(c.kind, json.RawMessage(c.raw))); got != c.want {
			t.Errorf("%s %s: %s, want %s", c.kind, c.raw, got, c.want)
		}
	}
}

// servedBefore is every data key /v1/events served before the key filter,
// by kind, beyond the full-width tape (testscale.TapeEvents): the week's
// rows (testscale.Week.EventsSQL), which no producer writes this way.
var servedBefore = map[string][]string{
	"pulse_down":             {"source", "p50_ms", "baseline_ms"},
	"pulse_recovered":        {"source", "p50_ms", "baseline_ms"},
	"latency_rise":           {"source", "p50_ms", "baseline_ms"},
	"estimate_hour_released": {"day", "hour", "formula_version", "unique_ips", "requests"},
}

// droppedKeys are the served keys the filter drops, each with why
// (api/CHANGES.md lists them).
var droppedKeys = map[string]string{
	"pulse_down.source": "the week's own rows", "pulse_down.p50_ms": "the week's own rows",
	"pulse_down.baseline_ms": "the week's own rows", "pulse_recovered.source": "the week's own rows",
	"pulse_recovered.p50_ms": "the week's own rows", "pulse_recovered.baseline_ms": "the week's own rows",
	"latency_rise.source": "the week's own rows", "latency_rise.p50_ms": "the week's own rows",
	"latency_rise.baseline_ms": "the week's own rows", "estimate_hour_released.requests": "the week's own rows",
}

// Every data key served on the full-width tape and the week stays served,
// unless droppedKeys says why not, so nothing the website reads disappears;
// and every value key of every kind is kept (registeredValues drops none).
func TestServedDataKeysStay(t *testing.T) {
	t.Parallel()
	served := map[string][]string{}
	for _, e := range testscale.TapeEvents(3) {
		var d map[string]any
		if err := json.Unmarshal(e.Data, &d); err != nil {
			t.Fatal(err)
		}
		served[e.Kind] = append(served[e.Kind], slices.Collect(maps.Keys(d))...)
	}
	week := testscale.Week.EventsSQL()
	for kind, list := range servedBefore {
		for _, k := range list {
			if !strings.Contains(week, "'"+kind+"'") || !strings.Contains(week, k) {
				t.Errorf("the week serves no %s.%s: update servedBefore", kind, k)
			}
		}
		served[kind] = append(served[kind], list...)
	}
	used := map[string]bool{}
	for kind, list := range served {
		for _, k := range list {
			if _, ok := dataRules[kind][k]; ok {
				continue
			}
			if droppedKeys[kind+"."+k] == "" {
				t.Errorf("%s.%s was served and is dropped: register it in eventData or list it in droppedKeys", kind, k)
			}
			used[kind+"."+k] = true
		}
	}
	for k := range droppedKeys {
		if !used[k] {
			t.Errorf("droppedKeys lists %s, which is not dropped: remove it", k)
		}
	}
	for kind, fn := range valueMakers {
		v := fn(map[string]any{})
		if kept := registeredValues(kind, maps.Clone(v)); len(kept) != len(v) {
			t.Errorf("%s: values %v keeps only %v", kind, slices.Sorted(maps.Keys(v)), slices.Sorted(maps.Keys(kept)))
		}
	}
}

// Every registered kind is an event code's, and every event code has
// its data keys.
func TestEventDataMatchesTheCodes(t *testing.T) {
	t.Parallel()
	for kind := range eventData {
		if !IsEvent(kind) {
			t.Errorf("eventData registers %s, no event code", kind)
		}
	}
	for _, code := range EventCodeNames() {
		if _, ok := eventData[EventKind(code)]; !ok {
			t.Errorf("event %s has no data keys in eventData", code)
		}
	}
	if !IsEvent("hour_released") || !IsEvent("estimate_hour_released") || IsEvent("census") || IsEvent("nope") {
		t.Error("IsEvent")
	}
}
