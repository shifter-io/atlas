package server

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/shifter-io/atlas/api/internal/build"
	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

// The success-rate scan, inverted (SUCCESS-RATE-REMOVE-1, #900): #898
// checked that every success rate had its 1-10 score beside it; the rates
// are now internal, so no answer, list page or the dataset file serves a
// success, CAPTCHA or block rate at all. The removed fields are api/FIELDS.md's
// "Success scores instead of success rates" table. The home's rate curves
// went later (SUCCESS-CURVE-1, #1072): no key named curve remains, and no
// tape or event row of a success kind (successEvents) serves values.value,
// values.baseline or values.baselineRate.

// removedRates are the removed rate keys, wherever they are.
var removedRates = map[string]bool{"successRate": true, "requestSuccessRate": true, "medianSuccess": true, "medianSuccessRate": true,
	"meanSuccess": true, "successSpark": true, "successDeltaPts": true, "successRateDelta24h": true, "successRateDelta30d": true,
	"troughSuccessRate": true, "captchaRate": true, "blockRate": true, "successSeries": true, "success24h": true, "success30d": true,
	"trend": true, "curve": true, "baselineRate": true}

// successEvents are the event codes whose values.value and values.baseline
// were success shares (#1072): their rows keep only the counts.
var successEvents = map[string]bool{"pulse_finished": true, "down": true, "success_drop": true, "recovered": true}

// rateNames are other keys that name a success rate: any *successRate*, or
// a key ending in Success (medianSuccess) that is no count or cost.
var rateNames = regexp.MustCompile(`(?i)successRate|^[a-z]+Success$`)

// notRates are keys rateNames matches that are no removed rate: the requests
// one success takes, the plain-HTTP cost of 1,000 successes and the one rate
// the owner brought back, the success rate behind the 8-target bill
// (SUCCESS-ALL-1, 2026-11-18: the true-cost table's Success column).
var notRates = map[string]bool{"attemptsPerSuccess": true, "costPer1kSuccess": true, "successRateAllTargets": true}

// parentRates are rate keys by the key of the object they are in: the
// success share of an outcome mix, the events' pass rate and the most
// reliable network's rate.
var parentRates = map[string]string{"outcomes": "ok", "pass": "rate", "mostReliable": "value"}

// byKey are the objects keyed by a range, target, engine, kind or path:
// their keys are listed as <key>, so that one gap is listed once.
var byKey = map[string]bool{"ranges": true, "stats": true, "cells": true, "fleet": true, "engines": true, "scoreStatus": true,
	"productMeasurements": true, "byTarget": true, "browserOverall": true, "lists": true, "targets": true}

func label(k, parent string) string {
	if parent == "pages" {
		return bodyGroup(k)
	}
	if byKey[parent] {
		return "<key>"
	}
	return k
}

var positions = regexp.MustCompile(`\[[^\]]*\]`)

func normalize(path string) string { return positions.ReplaceAllString(path, "[]") }

// scanRates lists, in v (a body's generic JSON), every rate key it serves
// (rateKey), by its path; a naReasons key is a path, scanned as one.
func scanRates(v any, path, parent string, found map[string]bool) {
	switch x := v.(type) {
	case map[string]any:
		successValues(x, path, found)
		for k, child := range x {
			p := path + "." + label(k, parent)
			if rateKey(k, parent, child) || parent == "naReasons" && rateInPath(k) {
				found[normalize(p)] = true
			}
			scanRates(child, p, k, found)
		}
	case []any:
		for _, child := range x {
			scanRates(child, path+"[]", parent+"[]", found)
		}
	}
}

// successValues lists a tape or event row x of a success kind that serves
// values.value or values.baseline, by its path and event.
func successValues(x map[string]any, path string, found map[string]bool) {
	event, _ := x["event"].(string)
	values, ok := x["values"].(map[string]any)
	if !successEvents[event] || !ok {
		return
	}
	for _, k := range []string{"value", "baseline"} {
		if _, has := values[k]; has {
			found[normalize(path)+".values."+k+" ("+event+")"] = true
		}
	}
}

// rateKey is whether key k (of an object whose own key is parent) serves a
// rate: a removed key, a rate name, a parent's rate key, or the rate (v) of
// a success history's point.
func rateKey(k, parent string, child any) bool {
	switch {
	case removedRates[k], rateNames.MatchString(k) && !notRates[k], parentRates[parent] == k:
		return true
	case k == "success":
		points, ok := child.([]any)
		return ok && len(points) > 0 && hasKey(points[0], "v")
	}
	return false
}

// rateInPath is whether a path (an N/A path, or a gap check's, whose
// optional keys end in ?) names a removed rate key or a parent's rate key.
func rateInPath(p string) bool {
	parent := ""
	for _, k := range strings.Split(normalize(p), ".") {
		k = strings.TrimSuffix(strings.TrimSuffix(k, "?"), "[]")
		if removedRates[k] || rateNames.MatchString(k) && !notRates[k] || parentRates[parent] == k {
			return true
		}
		parent = k
	}
	return false
}

// No gap of docs/api-web-map-gaps.json checks a removed rate key: siteapi
// bench --gaps would report it missing against every build.
func TestNoMapGapChecksARemovedRate(t *testing.T) {
	t.Parallel()
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "docs", "api-web-map-gaps.json"))
	if err != nil {
		t.Fatal(err)
	}
	var m struct {
		Gaps []struct {
			ID     string `json:"id"`
			Checks []struct {
				Paths []string `json:"paths"`
			} `json:"checks"`
		} `json:"gaps"`
	}
	if err := json.Unmarshal(raw, &m); err != nil || len(m.Gaps) == 0 {
		t.Fatalf("manifest: %d gaps, %v", len(m.Gaps), err)
	}
	for _, g := range m.Gaps {
		for _, c := range g.Checks {
			for _, p := range c.Paths {
				if rateInPath(p) {
					t.Errorf("%s checks %s, a removed rate", g.ID, p)
				}
			}
		}
	}
	for _, p := range []string{"pass.rate", "networks[].cells.*.captchaRate?", "x.outcomes.ok"} {
		if !rateInPath(p) {
			t.Errorf("rateInPath(%q) is false", p)
		}
	}
	if rateInPath("pass.successScore") || rateInPath("stats.*.attemptsPerSuccess") {
		t.Error("a score or attemptsPerSuccess counted as a rate")
	}
}

func hasKey(v any, k string) bool {
	m, ok := v.(map[string]any)
	_, has := m[k]
	return ok && has
}

// scanShape is a build with every scored field filled: the browser targets
// and the success scores, test events (and a full tape, below), unmeasured
// networks and network lists.
var scanShape = testscale.NetworkShape{Shape: testscale.Shape{Networks: 6, Countries: 1, Exits: 12, Tail: 12, Unmeasured: 1, Events: 40,
	Browser: true, SuccessScores: true, CityAsn: 20}, Requested: 3}

// TestNoAnswerServesASuccessRate walks every answer, a page of every long
// list and the dataset file of the scan build, and fails on any success,
// CAPTCHA or block rate still served.
func TestNoAnswerServesASuccessRate(t *testing.T) {
	t.Parallel()
	found := map[string]bool{}
	for name, b := range scanBodies(t) {
		var doc any
		if json.Unmarshal(b, &doc) != nil {
			continue // not a JSON body
		}
		scanRates(doc, bodyGroup(name), "", found)
	}
	if len(found) > 0 {
		list := make([]string, 0, len(found))
		for g := range found {
			list = append(list, g)
		}
		sort.Strings(list)
		t.Errorf("%d success, CAPTCHA or block rates served:\n%s", len(list), strings.Join(list, "\n"))
	}
}

// The scan finds each kind of rate key, and passes the scores, counts and
// costs beside them.
func TestTheRateScanFindsEveryKindOfRate(t *testing.T) {
	t.Parallel()
	var doc any
	body := `{"stats":{"24h":{"successRate":0.9,"successScore":{"score":7},"successes":9,"attemptsPerSuccess":1.1,"costPer1kSuccess":2}},
		"networks":[{"requestSuccessRate":0.9,"trend":[0.9],"curve":{"values":[0.9]},"successScoreCurve":{"values":[9.1]},"outcomes":{"ok":0.9,"challenge":0.1},"medianSuccess":0.9}],
		"market":{"mostReliable":{"value":0.9,"score":10},"meanSuccessShare":1},"pass":{"rate":0.9,"successScore":{}},
		"chart":{"success":[{"t":1,"v":0.9,"score":7}],"availability":[{"t":1,"v":1}]},"x":{"success":[{"t":1,"score":7}]},
		"tape":[{"event":"down","values":{"requests":9,"successes":0,"value":0,"baseline":null,"baselineRate":0.9}},
			{"event":"latency_spike","values":{"value":900,"baseline":400}},{"event":"recovered","values":{"requests":9,"successes":9}}],
		"naReasons":{"stats.24h.captchaRate":"insufficient_data","successScore.score":"ranked"}}`
	if err := json.Unmarshal([]byte(body), &doc); err != nil {
		t.Fatal(err)
	}
	found := map[string]bool{}
	scanRates(doc, "", "", found)
	want := []string{".chart.success", ".market.mostReliable.value", ".naReasons.stats.24h.captchaRate", ".networks[].curve", ".networks[].medianSuccess",
		".networks[].outcomes.ok", ".networks[].requestSuccessRate", ".networks[].trend", ".pass.rate", ".stats.<key>.successRate",
		".tape[].values.baseline (down)", ".tape[].values.baselineRate", ".tape[].values.value (down)"}
	got := make([]string, 0, len(found))
	for p := range found {
		got = append(got, p)
	}
	sort.Strings(got)
	if strings.Join(got, " ") != strings.Join(want, " ") {
		t.Errorf("found %v, want %v", got, want)
	}
}

// scanBodies is every answer, a page of every long list (scanPages) and the
// dataset file of the scan build (scanShape), by path ("dataset file" for
// the file).
func scanBodies(t *testing.T) map[string][]byte {
	t.Helper()
	bodies, _ := scanServer(t)
	return bodies
}

// scanServer is scanBodies and the server that answers them.
func scanServer(t *testing.T) (map[string][]byte, *Server) {
	t.Helper()
	in, err := testscale.NetworkInputs(scanShape)
	if err != nil {
		t.Fatal(err)
	}
	return serveScan(t, in, scanShape.Networks)
}

// serveScan is scanServer of in, with a community run, a scope and a full
// tape over networks.
func serveScan(t *testing.T, in *store.Inputs, networks int) (map[string][]byte, *Server) {
	t.Helper()
	requests, rate := int64(500), 0.9
	in.CommunityRuns = []store.CommunityRunRow{{ID: "run1", ProviderSlug: "net00", ProductKind: "residential_rotating", Status: "accepted",
		Requests: &requests, SuccessRate: &rate, Targets: json.RawMessage(`[{"target":"generic_http","successRate":0.9}]`),
		SuccessSeries: json.RawMessage(`[{"t":0,"v":0.9}]`), SubmittedAt: time.Now()}}
	// A full tape of every kind, on the tapes and in /v1/events, so every
	// key of a tape row's values is scanned (TAPE-VALUES-2).
	// A scope, so the scans walk /v1/meta's scopes (#966).
	in.Scopes = append(in.Scopes, store.ScopeRow{Kind: "residential_rotating", AllTargets: true, GeoCoverage: model.S("all_usable_countries")})
	// The HTTP targets without their catalog rows (the methodology's groups
	// and browser targets need theirs), so the scans walk the catalog's N/A
	// reasons in /v1/coverage.
	in.Targets = slices.DeleteFunc(in.Targets, func(r store.TargetRow) bool { return strings.HasPrefix(r.ID, "generic_") })
	// A catalog row with the provider facts as codes (PROVIDER-CODES-1), so
	// the scans walk hqCountry, hqCity and useCases.
	in.Providers = append(in.Providers, store.ProviderRow{Slug: "net00", Name: "Net 00", FrameworkProvider: model.S("net00"), Listed: true,
		HQCountry: model.S("LT"), HQCity: model.S("Vilnius"), UseCases: []string{"web_scraping", "ad_verification"}})
	// Its pricing with the trial in numbers and a plan with its audience and
	// pool kinds (PRICING-CODES-1), so the scans walk them and the plan's N/A
	// paths, which name it by its index.
	price, gb, days := 5.0, 10.0, int32(3)
	in.Pricing = append(in.Pricing, store.PricingRow{ProviderSlug: "net00", Category: "residential", Model: model.S("per_gb"),
		PricePerGb: &price, TrialPrice: &price, TrialCredit: new(float64), TrialGb: &gb, TrialDays: &days})
	in.Plans = append(in.Plans, store.PlanRow{ProviderSlug: "net00", Category: "residential", Name: model.S("Starter"), Price: &price, Gb: &gb,
		Catalog: json.RawMessage(`{"pool": "Full Geo", "poolKinds": ["residential", "mobile"], "billing": "monthly", "discountSource": "volume", "audience": "new_customers", "bandwidth": "10 GB", "period": "/month"}`)})
	tape := testscale.TapeEvents(networks)
	in.TapeHeads, in.Events = tape, append(append([]store.Event{}, tape...), in.Events...)
	sort.SliceStable(in.Events, func(i, j int) bool { return in.Events[i].At.After(in.Events[j].At) })
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	v, err := RenderVersion(1, res, Options{FilesDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	bodies := map[string][]byte{"dataset file": fileBytes(t, v.Dataset)}
	for p, r := range v.Static {
		bodies[p] = r.Identity
	}
	srv := New(&fakeSource{}, Options{})
	srv.current.Store(v)
	h := srv.Handler()
	for _, path := range scanPages(res) {
		if rec := get(h, path); rec.Code == 200 {
			bodies[path] = rec.Body.Bytes() // a network without the list answers 404
		}
	}
	return bodies, srv
}

// scanPages is a page of every network's place lists in every range and
// dimension, and a Coverage page.
func scanPages(res *build.Result) []string {
	out := []string{"/v1/events/stats", "/v1/events/stats?range=7d", "/v1/networks/net00/runs", "/v1/networks/net00/coverage"}
	for _, n := range res.Pages.Networks.Networks {
		for _, r := range build.Ranges {
			for _, d := range []string{"country", "region", "city", "asn"} {
				out = append(out, fmt.Sprintf("/v1/networks/%s/places?dimension=%s&range=%s", n.Slug, d, r.ID))
			}
		}
	}
	return out
}

// bodyGroup names a body by its path with its network (unless the
// all-networks answer) as * and without its query, so that one gap is
// listed once.
func bodyGroup(name string) string {
	name, _, _ = strings.Cut(name, "?")
	parts := strings.Split(name, "/")
	if len(parts) > 3 && parts[3] != "all" {
		parts[3] = "*"
	}
	return strings.Join(parts, "/")
}
