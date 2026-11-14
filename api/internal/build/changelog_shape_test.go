package build

import (
	"encoding/json"
	"fmt"
	"maps"
	"slices"
	"strings"
	"testing"
)

// changeShape is changelog-data.ts: every object's fields and each field's
// JSON types ("null" where the type allows it, "absent" where it may be
// left out).
var changeShape = map[string]map[string]string{
	"record": {"generatedAt": "string", "runId": "string null", "weekSeconds": "number", "entries": "array", "movers": "array"},
	"entry": {"id": "string", "kind": "string", "event": "string", "network": "object null", "severity": "string", "agoSeconds": "number", "source": "string", "incident": "object null",
		"values": "object", "naReasons": "object absent"},
	"network": {"slug": "string", "name": "string"},
	"incident": {"severity": "string", "troughSuccessRate": "number null", "impact": "number null", "durationMinutes": "number null",
		"countries": "number", "countryCodes": "array", "kind": "string", "detectedBy": "string", "ongoing": "boolean", "startedAt": "string null", "endedAt": "string null"},
	"mover": {"slug": "string", "name": "string", "source": "string", "rank": "number", "previousRank": "number", "delta": "number", "reasonId": "string null"},
}

func jsonType(v any) string {
	switch v.(type) {
	case nil:
		return "null"
	case string:
		return "string"
	case float64:
		return "number"
	case []any:
		return "array"
	case bool:
		return "boolean"
	}
	return "object"
}

// checkShape reports every field of obj missing from shape, outside it or of
// another type.
func checkShape(t *testing.T, where, shape string, obj any) map[string]any {
	t.Helper()
	m, _ := obj.(map[string]any)
	want := changeShape[shape]
	for k, types := range want {
		v, ok := m[k]
		if !ok && strings.Contains(types, "absent") {
			continue
		}
		if !ok || !strings.Contains(types, jsonType(v)) {
			t.Errorf("%s: %s is %v (%s), want %s", where, k, v, jsonType(v), types)
		}
	}
	for k := range m {
		if _, ok := want[k]; !ok {
			t.Errorf("%s: extra field %s", where, k)
		}
	}
	return m
}

// Every body decodes field for field as the website's ChangeRecord: no field
// missing, none extra (no title, detail, delta or unit since #990), each of
// its type; kinds and severities are the page's.
func TestTheChangelogHasTheWebsiteShape(t *testing.T) {
	t.Parallel()
	kinds := []string{"incident", "price", "pool", "performance", "authenticity", "coverage", "methodology"}
	for path, body := range changeBodies(t, changeFixture()) {
		var rec any
		if err := json.Unmarshal(body, &rec); err != nil {
			t.Fatal(err)
		}
		r := checkShape(t, path, "record", rec)
		entries, _ := r["entries"].([]any)
		for i, e := range entries {
			en := checkShape(t, fmt.Sprintf("%s entries[%d]", path, i), "entry", e)
			if !slices.Contains(kinds, en["kind"].(string)) || !slices.Contains([]string{"info", "good", "bad"}, en["severity"].(string)) {
				t.Errorf("%s entries[%d]: kind %v, severity %v", path, i, en["kind"], en["severity"])
			}
			checkValues(t, fmt.Sprintf("%s entries[%d]", path, i), en)
			for _, f := range []string{"network", "incident"} {
				if en[f] != nil {
					checkShape(t, fmt.Sprintf("%s entries[%d].%s", path, i, f), f, en[f])
				}
			}
		}
		movers, _ := r["movers"].([]any)
		for i, m := range movers {
			checkShape(t, fmt.Sprintf("%s movers[%d]", path, i), "mover", m)
		}
	}
}

// valueKeys is the website's values keys of each entry kind.
var valueKeys = map[string][]string{
	"pool": {"deltaPct", "previousUniqueIps", "uniqueIps"}, "price": {"deltaPct", "fromRate", "plan", "toRate"},
	"performance":  {"delta", "metric", "previous", "value"},
	"coverage":     {"comparedRange", "countries", "delta", "lostCountryCodes", "previousCountries"},
	"authenticity": {"delta", "previous", "value"}, "methodology": {"fromId", "toId"}, "incident": {},
}

// checkValues reports an entry whose values keys are not its kind's nor
// its event's parameters (a catalog row's all null), or a null value
// without its reason in naReasons (and a reason without one).
func checkValues(t *testing.T, where string, en map[string]any) {
	t.Helper()
	values, _ := en["values"].(map[string]any)
	keys := slices.Sorted(maps.Keys(values))
	if want := valueKeys[en["kind"].(string)]; !slices.Equal(keys, want) {
		t.Errorf("%s: values keys %v, want %v", where, keys, want)
	}
	if params, ok := eventParams()[en["event"].(string)]; !ok || en["event"] != "catalog" && !slices.Equal(keys, params) {
		t.Errorf("%s: event %v (params %v, registered %v) for values keys %v", where, en["event"], params, ok, keys)
	}
	reasons, _ := en["naReasons"].(map[string]any)
	nulls := 0
	for k, v := range values {
		if v == nil {
			nulls++
			if _, ok := reasons["values."+k].(string); !ok {
				t.Errorf("%s: values.%s is null without a reason", where, k)
			}
		}
	}
	if len(reasons) != nulls || en["event"] == "catalog" && nulls != len(values) {
		t.Errorf("%s: naReasons %v for %d nulls of %d", where, reasons, nulls, len(values))
	}
}

// eventParams is each change_event code's parameter names, sorted.
func eventParams() map[string][]string {
	out := map[string][]string{}
	for _, c := range codeSets["change_event"].Values {
		names := []string{}
		for _, p := range c.Params {
			names = append(names, p.Name)
		}
		out[c.Code] = slices.Sorted(slices.Values(names))
	}
	return out
}
