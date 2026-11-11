package build

import (
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"testing"
)

// changeShape is changelog-data.ts: every object's fields and each field's
// JSON types ("null" where the type allows it).
var changeShape = map[string]map[string]string{
	"record":   {"generatedAt": "string", "runId": "string null", "weekSeconds": "number", "entries": "array", "movers": "array"},
	"entry":    {"id": "string", "kind": "string", "network": "object null", "title": "object null", "detail": "object null", "delta": "number null", "unit": "string", "severity": "string", "agoSeconds": "number", "source": "string", "incident": "object null"},
	"network":  {"slug": "string", "name": "string"},
	"template": {"key": "string", "params": "object"},
	"incident": {"severity": "string", "scope": "string", "summary": "string", "troughSuccessRate": "number null", "impact": "number null", "durationMinutes": "number null"},
	"mover":    {"slug": "string", "name": "string", "source": "string", "rank": "number", "previousRank": "number", "delta": "number", "reasonId": "string null"},
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
// missing, none extra, each of its type; kinds and severities are the
// page's; every template param is a string.
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
			for _, f := range []string{"network", "title", "detail", "incident"} {
				if en[f] == nil {
					continue
				}
				shape := map[string]string{"network": "network", "title": "template", "detail": "template", "incident": "incident"}[f]
				sub := checkShape(t, fmt.Sprintf("%s entries[%d].%s", path, i, f), shape, en[f])
				if params, ok := sub["params"].(map[string]any); ok {
					for k, v := range params {
						if _, isString := v.(string); !isString {
							t.Errorf("%s entries[%d].%s.params.%s is %v", path, i, f, k, v)
						}
					}
				}
			}
		}
		movers, _ := r["movers"].([]any)
		for i, m := range movers {
			checkShape(t, fmt.Sprintf("%s movers[%d]", path, i), "mover", m)
		}
	}
}
