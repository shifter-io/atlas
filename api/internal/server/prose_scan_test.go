package server

import (
	"encoding/json"
	"net/http"
	"os"
	"sort"
	"strings"
	"testing"
	"time"
)

// The prose scan (API-PROSE-REMOVAL-1, #962): the website writes every line
// from codes and values (its commit 168c7957), so no answer, list page or
// dataset file serves the English fields it no longer reads. Its source is
// api/FIELDS.md "Removed on 2026-11-13 (prose)". It walks the same bodies as
// #898's success-rate scan (scanBodies).

// proseKeys are keys no body may carry anywhere.
var proseKeys = map[string]bool{
	"tagline": true, "claim": true, "verdict": true, "strengths": true, "weaknesses": true, "schedule": true,
	"latencyNote": true, "priceLabel": true, "stickinessAdvertised": true, "costBasisAllTargets": true,
	"scoreDefinitions": true, "poolDescription": true, "rationale": true, "headline": true, "phrase": true,
	"resolution": true,
}

// prosePaths are the removed fields whose key other places still use, as the
// end of a scanned path: a tape row's text, an incident's scope and summary,
// a plan's or product's note and the plan catalog's features, a ranking
// profile's description, a range's label and bucket, a scope's text and the
// network protocol's cadence. The changelog entries' title and detail stay,
// as codes.
var prosePaths = []string{"tape[].title", "tape[].detail", "tape[].value", "events[].title", "events[].detail",
	"events[].value", "incident.scope", "incident.summary", "incidents[].scope", "incidents[].summary",
	"plans[].note", "products[].note", "catalog.features", "standing.note", "profiles[].description",
	"range.label", "range.bucket", "ranges[].label", "ranges[].bucket", "scopes[].label", "scopes[].summary",
	"protocol.cadence"}

// proseOf lists, in v (a body's generic JSON), every path of a removed prose
// field into found and every path into seen, as retiredOf does.
func proseOf(v any, path, parent string, found, seen map[string]bool) {
	switch x := v.(type) {
	case map[string]any:
		for k, child := range x {
			p := path + "." + label(k, parent)
			seen[normalize(p)] = true
			if isProse(k, normalize(p), child) {
				found[normalize(p)] = true
			}
			proseOf(child, p, k, found, seen)
		}
	case []any:
		for _, child := range x {
			proseOf(child, path+"[]", parent+"[]", found, seen)
		}
	}
}

// isProse is whether key k at path p, of value v, is a removed prose field;
// a pricing history's basis is its top-level text, and a scope's cadence and
// geoCoverage are objects of numbers and codes now, never text.
func isProse(k, p string, v any) bool {
	if proseKeys[k] {
		return true
	}
	for _, r := range prosePaths {
		if strings.HasSuffix(p, "."+r) {
			return true
		}
	}
	if _, text := v.(string); text && (k == "cadence" || k == "geoCoverage") {
		return true
	}
	return k == "basis" && strings.HasSuffix(p, "/pricing-history.basis")
}

// scanProse is every removed prose field's path in bodies, sorted, and every
// path seen.
func scanProse(bodies map[string][]byte) (found []string, seen map[string]bool) {
	hits := map[string]bool{}
	seen = map[string]bool{}
	for name, b := range bodies {
		var doc any
		if json.Unmarshal(b, &doc) != nil {
			continue // not a JSON body
		}
		proseOf(doc, bodyGroup(name), "", hits, seen)
	}
	for p := range hits {
		found = append(found, p)
	}
	sort.Strings(found)
	return found, seen
}

// TestNoRemovedProseField walks every answer, a page of every long list and
// the dataset file of the scan build, and fails on any removed prose field;
// the codes, names and values that replace them must still be served.
func TestNoRemovedProseField(t *testing.T) {
	t.Parallel()
	found, seen := scanProse(scanBodies(t))
	if len(found) > 0 {
		t.Errorf("%d removed prose fields served:\n%s", len(found), strings.Join(found, "\n"))
	}
	for _, want := range []string{"/v1/home/*.tape[].values", ".scopes[].cadence.dailyTestsPerCountry", ".scopes[].geoCoverage.code",
		".ranges[].axis", "/v1/rankings/*.networks[].trialKind", ".profile.protocol.summary", ".profile.pricing.trial",
		".profile.bestFor", ".profile.incidents"} {
		if !seenEnding(seen, want) {
			t.Errorf("no body serves %s: the codes and values stay", want)
		}
	}
}

func TestProseMatchesOnlyRemovedFields(t *testing.T) {
	t.Parallel()
	doc := map[string]any{
		"tape":     []any{map[string]any{"title": "x", "value": "1%", "values": map[string]any{"value": 0.5}}},
		"entries":  []any{map[string]any{"title": map[string]any{"key": "pool_grew"}, "incident": map[string]any{"scope": "AD", "severity": "major"}}},
		"profile":  map[string]any{"tagline": "x", "protocol": map[string]any{"summary": "y", "cadence": "z"}, "bestFor": []any{"a"}},
		"scopes":   []any{map[string]any{"label": "x", "cadence": map[string]any{"dailyTestsPerCountry": 1}, "geoCoverage": "all"}},
		"profiles": []any{map[string]any{"label": "Hard targets", "description": "x"}},
	}
	b, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	found, _ := scanProse(map[string][]byte{"/v1/x": b, "/v1/networks/a/pricing-history": []byte(`{"basis":"x","points":[]}`)})
	want := []string{"/v1/networks/*/pricing-history.basis", "/v1/x.entries[].incident.scope", "/v1/x.profile.protocol.cadence",
		"/v1/x.profile.tagline", "/v1/x.profiles[].description", "/v1/x.scopes[].geoCoverage", "/v1/x.scopes[].label",
		"/v1/x.tape[].title", "/v1/x.tape[].value"}
	if strings.Join(found, ",") != strings.Join(want, ",") {
		t.Errorf("found %v, want %v", found, want)
	}
}

// TestNoRemovedProseFieldLive runs the scan on a served API (SITEAPI_SCAN_URL,
// for example the public URL after a deploy): every range of the home and
// the rankings, the meta, events, changelog and targets answers, every
// network with its pricing history, and the dataset file.
func TestNoRemovedProseFieldLive(t *testing.T) {
	base := strings.TrimRight(os.Getenv("SITEAPI_SCAN_URL"), "/")
	if base == "" {
		t.Skip("SITEAPI_SCAN_URL not set")
	}
	client := &http.Client{Timeout: 60 * time.Second}
	bodies := map[string][]byte{}
	paths := []string{"/v1/targets", "/v1/dataset", "/v1/networks", "/v1/meta", "/v1/events", "/v1/changelog", "/v1/search-index"}
	for _, r := range []string{"24h", "7d", "30d"} {
		paths = append(paths, "/v1/home?range="+r, "/v1/rankings?range="+r)
	}
	for _, p := range paths {
		bodies[p] = fetch(t, client, base+p)
	}
	var list struct {
		Networks []struct {
			Slug string `json:"slug"`
		} `json:"networks"`
	}
	if err := json.Unmarshal(bodies["/v1/networks"], &list); err != nil || len(list.Networks) == 0 {
		t.Fatalf("/v1/networks: %d networks, %v", len(list.Networks), err)
	}
	for _, n := range list.Networks {
		for _, p := range []string{"/v1/networks/" + n.Slug, "/v1/networks/" + n.Slug + "/pricing-history"} {
			bodies[p] = fetch(t, client, base+p)
		}
	}
	found, _ := scanProse(bodies)
	t.Logf("scanned %d bodies on %s", len(bodies), base)
	if len(found) > 0 {
		t.Errorf("%d removed prose fields served:\n%s", len(found), strings.Join(found, "\n"))
	}
}
