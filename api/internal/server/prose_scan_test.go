package server

import (
	"encoding/json"
	"net/http"
	"os"
	"slices"
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

// proseKeys are keys no body may carry anywhere: among them the point labels
// of every series and a range's axis words and short form
// (TIME-LABELS-REMOVE-1, #988; the chart answers' labels went in #977).
var proseKeys = map[string]bool{
	"tagline": true, "claim": true, "verdict": true, "strengths": true, "weaknesses": true, "schedule": true,
	"latencyNote": true, "priceLabel": true, "stickinessAdvertised": true, "costBasisAllTargets": true,
	"scoreDefinitions": true, "poolDescription": true, "rationale": true, "headline": true, "phrase": true,
	"resolution": true, "passCondition": true, "labels": true, "axis": true, "short": true,
	// PROFILE-TEXT-REMOVE-1 (#991): a network's headquarters and best uses, a
	// review's verification label and spend text.
	"hq": true, "bestFor": true, "verificationLabel": true, "monthlySpend": true,
}

// prosePaths are the removed fields whose key other places still use, as the
// end of a scanned path: a tape row's text, an incident's scope and summary,
// a plan's or product's note and the plan catalog's features, a ranking
// profile's description, a range's label and bucket, a scope's label and
// summary (#966) and the network protocol's cadence; an outage band's label,
// an AI source's label and a product's name (UNREAD-TEXT-1, #977); a target's
// label and pass condition, the hardest target's and a ranking profile's label
// (TARGET-TEXT-REMOVE-1, #989); the plan catalog's bandwidth and period
// (PRICING-CODES-1, #987); the protocol's label and summary, the trial text,
// a leak check's detail and a community run's region (PROFILE-TEXT-REMOVE-1,
// #991; a runs answer's rows[].region in isProse).
var prosePaths = []string{"tape[].title", "tape[].detail", "tape[].value", "events[].title", "events[].detail",
	"events[].value", "incident.scope", "incident.summary", "incidents[].scope", "incidents[].summary",
	"plans[].note", "products[].note", "catalog.features", "standing.note", "profiles[].description",
	"range.label", "range.bucket", "ranges[].label", "ranges[].bucket", "scopes[].label", "scopes[].summary",
	"protocol.cadence", "bands[].label", "sources[].label", "products[].name",
	"targets[].label", "hardest.label", "profiles[].label", "catalog.bandwidth", "catalog.period",
	"protocol.label", "protocol.summary", "pricing.trial", "leakChecks[].detail", "communityRuns[].region"}

// changeText is the changelog entries' removed fields, of /v1/changelog
// entries[] and a network answer's changelog[] (CHANGELOG-TEXT-REMOVE-1,
// #990); the dataset file's own changelog[] rows keep delta and unit.
var changeText = map[string]bool{"title": true, "detail": true, "delta": true, "unit": true}

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
// a pricing history's basis is its top-level text, a changelog entry's
// title, detail, delta and unit are gone (changeText), and a scope's cadence
// and geoCoverage are objects of numbers and codes (#966), never text.
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
	if changeText[k] && (strings.HasSuffix(p, ".entries[]."+k) || strings.Contains(p, "/v1/networks/") && strings.HasSuffix(p, ".changelog[]."+k)) {
		return true
	}
	return k == "basis" && strings.HasSuffix(p, "/pricing-history.basis") || strings.HasSuffix(p, "/runs.rows[].region")
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
	for _, want := range []string{"/v1/home/*.tape[].values", "/v1/meta.ranges[].days", "/v1/meta.ranges[].hours", ".stats.<key>.range.days",
		".market.curve.times", ".networks[].curve.timesIndex", "measuredMarket.views[].headlineSpark.times", "/v1/meta.scopes[].cadence.browserTriesPerTargetPerDay",
		"/v1/meta.scopes[].geoCoverage.code", "dataset file.scopes[].countries", "/v1/rankings/*.networks[].trialKind", ".profile.protocol.targets", ".profile.pricing.trialKind",
		".profile.useCases", ".profile.hqCountry", ".profile.measuredProduct", ".profile.trust.leakChecks", ".profile.incidents", ".sources[].target", ".products[].kind", ".success[].t",
		"/v1/meta.targets[].category", "/v1/targets.targets[].id", "dataset file.targets[].category"} {
		if !seenEnding(seen, want) {
			t.Errorf("no body serves %s: the codes and values stay", want)
		}
	}
}

func TestProseMatchesOnlyRemovedFields(t *testing.T) {
	t.Parallel()
	doc := map[string]any{
		"tape": []any{map[string]any{"title": "x", "value": "1%", "values": map[string]any{"value": 0.5}}},
		"entries": []any{map[string]any{"title": map[string]any{"key": "pool_grew"}, "delta": 1, "unit": "%", "values": map[string]any{"delta": 1},
			"incident": map[string]any{"scope": "AD", "severity": "major"}}},
		"profile": map[string]any{"tagline": "x", "protocol": map[string]any{"summary": "y", "cadence": "z", "label": "x", "targets": []any{"t"}},
			"bestFor": []any{"a"}, "useCases": []any{"web_scraping"}, "hq": "x", "hqCountry": "LT", "pricing": map[string]any{"trial": "x", "trialKind": "free"},
			"trust": map[string]any{"leakChecks": []any{map[string]any{"name": "dns", "passed": true, "detail": "x"}}}},
		"reviews":       []any{map[string]any{"verificationLabel": "x", "monthlySpend": "$100", "monthlySpendMin": 100, "useCase": "web_scraping"}},
		"communityRuns": []any{map[string]any{"region": "eu", "country": "DE"}},
		"profiles":      []any{map[string]any{"label": "Hard targets", "description": "x"}},
		"targets":       []any{map[string]any{"label": "Product page", "passCondition": "x", "category": "ecommerce"}},
		"insights":      map[string]any{"hardest": map[string]any{"id": "ai_answers", "label": "x", "passCondition": "y"}},
		"scopes":        []any{map[string]any{"label": "x", "cadence": map[string]any{"dailyTestsPerNetworkCountryPerDay": 1}, "geoCoverage": "all"}},
		"products":      []any{map[string]any{"name": "Residential", "kind": "residential_rotating"}},
		"sources":       []any{map[string]any{"label": "AI answers: ChatGPT", "target": "ai_chatgpt"}},
		"labels":        []any{"1 day ago"},
		"market":        map[string]any{"curve": map[string]any{"labels": []any{"now"}, "times": []any{0}}},
		"ranges":        []any{map[string]any{"id": "24h", "short": "24h", "days": 1, "axis": []any{"now"}}},
	}
	b, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	chart := []byte(`{"bands":[{"label":"major · AD","severity":"major","country":"AD"}],"labels":{"success":["now"]}}`)
	network := []byte(`{"changelog":[{"event":"pool_grew","title":null,"detail":null,"delta":1,"unit":"%","values":{"delta":1}}]}`)
	dataset := []byte(`{"changelog":[{"kind":"pool","delta":1,"unit":"%"}],"pages":{"/v1/networks/a":` + string(network) + `}}`)
	found, _ := scanProse(map[string][]byte{"/v1/x": b, "/v1/networks/a/pricing-history": []byte(`{"basis":"x","points":[]}`),
		"/v1/charts/a/residential/24h": chart, "/v1/networks/a": network, "/v1/dataset": dataset,
		"/v1/networks/a/runs": []byte(`{"rows":[{"region":"eu","country":"DE"}]}`)})
	changelog := func(prefix string) []string {
		return []string{prefix + ".changelog[].delta", prefix + ".changelog[].detail", prefix + ".changelog[].title", prefix + ".changelog[].unit"}
	}
	want := slices.Concat(changelog("/v1/dataset.pages./v1/networks/*"), changelog("/v1/networks/*"), []string{
		"/v1/charts/*/residential/24h.bands[].label", "/v1/charts/*/residential/24h.labels", "/v1/networks/*/pricing-history.basis",
		"/v1/x.entries[].delta", "/v1/x.entries[].incident.scope", "/v1/x.entries[].title", "/v1/x.entries[].unit",
		"/v1/x.insights.hardest.label", "/v1/x.insights.hardest.passCondition",
		"/v1/x.labels", "/v1/x.market.curve.labels", "/v1/x.products[].name", "/v1/x.profile.protocol.cadence",
		"/v1/x.profile.tagline", "/v1/x.profiles[].description", "/v1/x.profiles[].label", "/v1/x.ranges[].axis", "/v1/x.ranges[].short",
		"/v1/x.scopes[].geoCoverage", "/v1/x.scopes[].label",
		"/v1/x.sources[].label", "/v1/x.tape[].title", "/v1/x.tape[].value", "/v1/x.targets[].label", "/v1/x.targets[].passCondition",
		"/v1/networks/*/runs.rows[].region", "/v1/x.communityRuns[].region", "/v1/x.profile.bestFor", "/v1/x.profile.hq", "/v1/x.profile.pricing.trial",
		"/v1/x.profile.protocol.label", "/v1/x.profile.protocol.summary", "/v1/x.profile.trust.leakChecks[].detail", "/v1/x.reviews[].monthlySpend", "/v1/x.reviews[].verificationLabel"})
	sort.Strings(want)
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
	paths := []string{"/v1/targets", "/v1/dataset", "/v1/networks", "/v1/meta", "/v1/events", "/v1/changelog"}
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
