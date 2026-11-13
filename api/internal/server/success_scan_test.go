package server

import (
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// The success-rate scan (SUCCESS-SCORE-ALL-1, #898): the website shows a
// 1-10 success score in place of every success rate, so every success rate
// an answer, a list page or the dataset file serves has its score next to
// it. Its source is the % -> score table of api/FIELDS.md ("Success scores
// instead of success rates").

// rateScore is each success-rate key and the sibling keys, any one of which
// is its score.
var rateScore = map[string][]string{
	"successRate":        {"successScore", "scores"},
	"requestSuccessRate": {"successScore"},
	"medianSuccess":      {"successScore"},
	"meanSuccess":        {"successScore"},
	"successSpark":       {"successScoreSpark"},
}

// notRates are keys with "success" in their name that are no success rate:
// counts, costs and the scores themselves.
var notRates = regexp.MustCompile(`^(successScore|successScoreSpark|successScoreDelta.*|.*[sS]uccesses.*|.*[cC]ost.*|.*Rank)$`)

// laterRates are rate keys the table gives to another issue, by key: the
// rate deltas' scores are #899's (successScoreDelta…); an incident's trough
// has no score (a dip of one network has no rank) and #900 removes it.
var laterRates = map[string]string{
	"successDeltaPts":     "#899",
	"successRateDelta24h": "#899",
	"successRateDelta30d": "#899",
	"deltaSuccessPts":     "#899",
	"troughSuccessRate":   "#900",
}

// scanExempt are the places (patterns of the paths, positions as [],
// matched anywhere in a path) whose success rates carry no score, with why.
var scanExempt = map[string]string{
	"/v1/charts/":                      "a network's chart series over time; not in the table (#898)",
	"/v1/countries/":                   "a country's rows: the dataset file has no room for their scores (#881)",
	"/v1/coverage":                     "names the fields a network fills, not their values",
	"/v1/networks/*/chart":             "a network's chart series over time; not in the table (#898)",
	"/v1/networks/all":                 "the all-networks answer: every network together, none to rank it against",
	".successSeries":                   "a community run's history series, as a chart's; not in the table (#898)",
	".profile.performance.successRate": "its score is profile.successScore (#881)",
	"dataset file.providers[].":        "the old site's raw network record: its score is providers[].successScore, its geo rows are a country's, its series a chart's",
	"dataset file.measuredMarket.":     "the old site's raw market views: /v1/home market serves them with their scores",
	"dataset file.measuredTotals.":     "the raw market totals: /v1/home market.successScore is their score",
	"dataset file.totals.":             "the raw market totals: /v1/home market.successScore is their score",
}

// skipped are keys whose values are no figures: the null lists and the
// ranks (a rank of a success rate is a position, not a rate).
var skipped = map[string]bool{"na": true, "naReason": true, "naReasons": true, "ranks": true, "labels": true}

// rateGap is one success rate without its score.
type rateGap struct{ body, path string }

// scanRates lists, in v (a body's generic JSON), every success rate without
// its score: a rateScore key without any of its score keys, pass.rate
// without pass.successScore, an outcome mix with an ok share and no scores
// beside it, and any other key naming success that is not a count, a cost,
// a score or a later issue's.
func scanRates(v any, path, parent string, gaps map[string]bool) {
	switch x := v.(type) {
	case map[string]any:
		for k, child := range x {
			if skipped[k] {
				continue
			}
			p := path + "." + label(k, parent)
			if gap := rateGapOf(x, k, child, parent); gap && !exempt(p) {
				gaps[normalize(p)] = true
			}
			scanRates(child, p, k, gaps)
		}
	case []any:
		for _, child := range x {
			scanRates(child, path+"[]", parent+"[]", gaps)
		}
	}
}

// rateGapOf is whether key k of object m (whose own key is parent) is a
// success rate without its score.
func rateGapOf(m map[string]any, k string, child any, parent string) bool {
	if scores, ok := rateScore[k]; ok {
		return !hasAny(m, scores)
	}
	switch {
	case k == "rate" && parent == "pass":
		return !hasAny(m, []string{"successScore"})
	case k == "outcomes":
		o, ok := child.(map[string]any)
		return ok && o["ok"] != nil && !hasAny(m, []string{"scores"})
	case k == "success":
		_, series := child.([]any)
		return series
	}
	_, later := laterRates[k]
	return strings.Contains(strings.ToLower(k), "success") && !notRates.MatchString(k) && !later
}

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

func hasAny(m map[string]any, keys []string) bool {
	for _, k := range keys {
		if _, ok := m[k]; ok {
			return true
		}
	}
	return false
}

var positions = regexp.MustCompile(`\[[^\]]*\]`)

func normalize(path string) string { return positions.ReplaceAllString(path, "[]") }

// exemptUsed are the scanExempt patterns that exempted a rate: one that
// exempts none is stale, and the scan fails on it.
var exemptUsed = map[string]bool{}

func exempt(path string) bool {
	p := normalize(path)
	for pattern := range scanExempt {
		if strings.Contains(p, pattern) {
			exemptUsed[pattern] = true
			return true
		}
	}
	return false
}

// scanShape is a build with every scored field filled: the browser targets
// and the success scores, test events (and a full tape, below), unmeasured
// networks and network lists.
var scanShape = testscale.NetworkShape{Shape: testscale.Shape{Networks: 6, Countries: 1, Exits: 12, Tail: 12, Unmeasured: 1, Events: 40,
	Browser: true, SuccessScores: true, CityAsn: 20}, Requested: 3}

// TestEverySuccessRateHasItsScore walks every answer, a page of every long
// list and the dataset file of the scan build, and fails on any success
// rate without its score.
func TestEverySuccessRateHasItsScore(t *testing.T) {
	t.Parallel()
	bodies := scanBodies(t)
	gaps := map[string]bool{}
	for name, b := range bodies {
		var doc any
		if json.Unmarshal(b, &doc) != nil {
			continue // not a JSON body
		}
		scanRates(doc, bodyGroup(name), "", gaps)
	}
	for pattern, why := range scanExempt {
		if !exemptUsed[pattern] {
			t.Errorf("exemption %q (%s) exempts no success rate: remove it", pattern, why)
		}
	}
	if len(gaps) > 0 {
		list := make([]string, 0, len(gaps))
		for g := range gaps {
			list = append(list, g)
		}
		sort.Strings(list)
		t.Errorf("%d success rates without their score:\n%s", len(list), strings.Join(list, "\n"))
	}
}

// scanBodies is every answer, a page of every long list (scanPages) and the
// dataset file of the scan build (scanShape), by path ("dataset file" for
// the file).
func scanBodies(t *testing.T) map[string][]byte {
	t.Helper()
	in, err := testscale.NetworkInputs(scanShape)
	if err != nil {
		t.Fatal(err)
	}
	requests, rate := int64(500), 0.9
	in.CommunityRuns = []store.CommunityRunRow{{ID: "run1", ProviderSlug: "net00", ProductKind: "residential_rotating", Status: "accepted",
		Requests: &requests, SuccessRate: &rate, Targets: json.RawMessage(`[{"target":"generic_http","successRate":0.9}]`),
		SuccessSeries: json.RawMessage(`[{"t":0,"v":0.9}]`), SubmittedAt: time.Now()}}
	// A full tape of every kind, on the tapes and in /v1/events, so every
	// key of a tape row's values is scanned (TAPE-VALUES-2).
	tape := testscale.TapeEvents(scanShape.Networks)
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
	return bodies
}

// scanPages is a page of every network's place lists in every range and
// dimension, and a region and a city page.
func scanPages(res *build.Result) []string {
	out := []string{"/v1/events/stats", "/v1/events/stats?range=7d", "/v1/networks/net00/runs"}
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
