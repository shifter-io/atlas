package server

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strings"
	"testing"
	"time"
)

// The retired plain-HTTP cost scan (HTTP-COST-RETIRE-1, #950): the
// all-targets cost replaced the plain-HTTP cost per 1,000 successes, so no
// answer, list page or dataset file serves it, its summaries or its inputs
// any more. Its source is api/FIELDS.md "Removed on 2026-11-13 (item 14)".
// The plain-HTTP value rank valueRank went with it (VALUE-ALL-RANK-1, #951):
// valueRankAllTargets is the one value rank.

// retiredKeys are keys no body may carry anywhere.
var retiredKeys = map[string]bool{
	"costBasis":                true,
	"wireGbPer1k":              true,
	"bills":                    true,
	"cheapestCostPer1kSuccess": true,
	"dearestCostPer1kSuccess":  true,
	"priceSpreadX":             true,
	"residentialSpreadX":       true,
	"valueRank":                true,
}

// retiredPaths are the retired fields whose key other places still use, as
// the end of a scanned path: the home's HTTP spread and bills and the
// rankings' HTTP cost summary (cost.allTargets stays).
var retiredPaths = []string{"market.spread", "insights.value", "cost.cheapest", "cost.dearest", "cost.median",
	"cost.spreadX", "cost.priced"}

// keptCosts are where costPer1kSuccess stays, with why: a per-target cell
// (one target, a real meaning) and the all-targets summary's ends (their
// costPer1kSuccess holds the all-targets bill).
var keptCosts = []string{"cells.<key>.costPer1kSuccess", "profile.targets[].costPer1kSuccess",
	"cost.allTargets.cheapest.costPer1kSuccess", "cost.allTargets.dearest.costPer1kSuccess"}

// retiredOf lists, in v (a body's generic JSON), every path of a retired
// field (normalized, keyed objects as <key>) into found, and every path into
// seen.
func retiredOf(v any, path, parent string, found, seen map[string]bool) {
	switch x := v.(type) {
	case map[string]any:
		for k, child := range x {
			p := path + "." + label(k, parent)
			seen[normalize(p)] = true
			if retired(k, normalize(p)) {
				found[normalize(p)] = true
			}
			retiredOf(child, p, k, found, seen)
		}
	case []any:
		for _, child := range x {
			retiredOf(child, path+"[]", parent+"[]", found, seen)
		}
	}
}

// retired is whether key k at path p is a retired field.
func retired(k, p string) bool {
	if retiredKeys[k] {
		return true
	}
	for _, r := range retiredPaths {
		if strings.HasSuffix(p, "."+r) {
			return true
		}
	}
	if k != "costPer1kSuccess" {
		return false
	}
	for _, kept := range keptCosts {
		if strings.HasSuffix(p, "."+kept) {
			return false
		}
	}
	return true
}

// scanRetired is every retired field's path in bodies, sorted, and every
// path seen.
func scanRetired(bodies map[string][]byte) (found []string, seen map[string]bool) {
	hits := map[string]bool{}
	seen = map[string]bool{}
	for name, b := range bodies {
		var doc any
		if json.Unmarshal(b, &doc) != nil {
			continue // not a JSON body
		}
		retiredOf(doc, bodyGroup(name), "", hits, seen)
	}
	for p := range hits {
		found = append(found, p)
	}
	sort.Strings(found)
	return found, seen
}

// TestNoRetiredHTTPCostField walks every answer, a page of every long list
// and the dataset file of the scan build, and fails on any retired
// plain-HTTP cost field; the fields that replace them must still be served.
func TestNoRetiredHTTPCostField(t *testing.T) {
	t.Parallel()
	found, seen := scanRetired(scanBodies(t))
	if len(found) > 0 {
		t.Errorf("%d retired plain-HTTP cost fields served:\n%s", len(found), strings.Join(found, "\n"))
	}
	for _, want := range []string{".cells.<key>.costPer1kSuccess", "/v1/rankings/*.cost.allTargets", "/v1/home/*.market.spreadAll",
		"/v1/home/*.insights.valueAll", "/v1/home/*.market.cheapest", "/v1/rankings/*.networks[].costPer1kAllTargets",
		"/v1/rankings/*.networks[].valueRankAllTargets", ".stats.<key>.ranks.valueRankAllTargets", ".stats.<key>.alternative"} {
		if !seenEnding(seen, want) {
			t.Errorf("no body serves %s: the replacements and kept fields stay", want)
		}
	}
}

func seenEnding(seen map[string]bool, suffix string) bool {
	for p := range seen {
		if strings.HasSuffix(p, suffix) {
			return true
		}
	}
	return false
}

func TestRetiredMatchesOnlyRetiredFields(t *testing.T) {
	t.Parallel()
	doc := map[string]any{
		"market":   map[string]any{"spread": 1, "spreadAll": 1, "cheapest": 1},
		"insights": map[string]any{"value": 1, "valueAll": 1},
		"cost":     map[string]any{"spreadX": 1, "allTargets": map[string]any{"cheapest": map[string]any{"costPer1kSuccess": 1}}},
		"networks": []any{map[string]any{"costPer1kSuccess": 1, "wireGbPer1k": 1, "valueRank": 1, "valueRankAllTargets": 1,
			"cells": map[string]any{"generic_http": map[string]any{"costPer1kSuccess": 1}}}},
		"profile": map[string]any{"pricing": map[string]any{"costBasis": "x", "costBasisAllTargets": "y"}},
	}
	b, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	found, _ := scanRetired(map[string][]byte{"/v1/x": b})
	want := []string{"/v1/x.cost.spreadX", "/v1/x.insights.value", "/v1/x.market.spread", "/v1/x.networks[].costPer1kSuccess",
		"/v1/x.networks[].valueRank", "/v1/x.networks[].wireGbPer1k", "/v1/x.profile.pricing.costBasis"}
	if strings.Join(found, ",") != strings.Join(want, ",") {
		t.Errorf("found %v, want %v", found, want)
	}
}

// TestNoRetiredHTTPCostFieldLive runs the scan on a served API
// (SITEAPI_SCAN_URL, for example the public URL after a deploy): every
// range of the home and the rankings, every network, the targets and the
// dataset file.
func TestNoRetiredHTTPCostFieldLive(t *testing.T) {
	base := strings.TrimRight(os.Getenv("SITEAPI_SCAN_URL"), "/")
	if base == "" {
		t.Skip("SITEAPI_SCAN_URL not set")
	}
	client := &http.Client{Timeout: 60 * time.Second}
	bodies := map[string][]byte{}
	paths := []string{"/v1/targets", "/v1/dataset", "/v1/networks"}
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
		p := "/v1/networks/" + n.Slug
		bodies[p] = fetch(t, client, base+p)
	}
	found, _ := scanRetired(bodies)
	t.Logf("scanned %d bodies on %s", len(bodies), base)
	if len(found) > 0 {
		t.Errorf("%d retired plain-HTTP cost fields served:\n%s", len(found), strings.Join(found, "\n"))
	}
}

// fetch is url's body (redirects followed); it fails t unless 200.
func fetch(t *testing.T, client *http.Client, url string) []byte {
	t.Helper()
	resp, err := client.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(resp.Body)
	if err != nil || resp.StatusCode != http.StatusOK {
		t.Fatal(fmt.Sprintf("%s: %d, %v", url, resp.StatusCode, err))
	}
	return b
}
