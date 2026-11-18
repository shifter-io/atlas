package store_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"testing"

	"github.com/shifter-io/atlas/api/internal/build"
	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

// This file uses only what main had before API-SUM-1, so it also shows
// main failing on a v4 build (the regression demonstration).

// netWindowLists are networkLists' list ids per window, and their row counts
// by breakdown name.
var (
	netWindowLists = map[string]int64{"24h": 1, "7d": 2, "30d": 3}
	netListCounts  = map[string]int{"country": 2, "region": 1, "city": 2, "asn": 3, "city_asn": 2}
)

// v4Network is the every-request fixture's census/pulse group template as
// provider p's network entry (contract K9), product pid, its windows naming
// networkLists' lists, its 30d window peering peers.
func v4Network(t *testing.T, template []byte, p string, pid int, peers []map[string]any) map[string]any {
	t.Helper()
	var g map[string]any
	if err := json.Unmarshal(template, &g); err != nil {
		t.Fatal(err)
	}
	g["id"], g["provider"], g["name"], g["product_id"], g["cohort"] = p+":residential:network", p, "Net "+p, pid, "network"
	g["criteria"] = map[string]any{"country": nil, "requested_countries": []string{"US"}, "groups": 1}
	for label, raw := range g["windows"].(map[string]any) {
		w := raw.(map[string]any)
		for _, k := range []string{"breakdowns", "breakdown_entries", "benchmark", "previous_period", "census_overlap", "latency",
			"evidence_sources", "unavailable", "roles"} {
			delete(w, k)
		}
		pool := w["pool"].(map[string]any)
		for _, k := range []string{"geo", "countries_breakdown", "asns_breakdown"} {
			delete(pool, k)
		}
		overlap := w["overlap"].(map[string]any)
		overlap["peers"] = []map[string]any{}
		if label == "30d" {
			overlap["peers"] = peers
		}
		w["lists"] = map[string]any{"id": netWindowLists[label], "entries": netListCounts}
	}
	return g
}

// v4Head is the fixture's head as payload v4 with networks shifter (product
// 1, peering acme on 77 IPs) and acme (product 2), and as the framework
// stores it: per-cohort overlap rows (cohortRows of them) and fleet rows,
// and the every-group fleet entries ("*", and "*:residential" of the kind).
func v4Head(t *testing.T, cohortRows int) []byte {
	t.Helper()
	raw, err := testscale.EveryRequest()
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	template, err := json.Marshal(doc["products"].([]any)[0])
	if err != nil {
		t.Fatal(err)
	}
	peer := func(p string, pid, ours, theirs int) []map[string]any {
		return []map[string]any{{"provider": p, "product_id": pid, "shared_ips": 77, "ips_ours": ours, "ips_theirs": theirs}}
	}
	doc["networks"] = []any{v4Network(t, template, "shifter", 1, peer("acme", 2, 700, 300)), v4Network(t, template, "acme", 2, peer("shifter", 1, 300, 700))}
	overlap := make([]any, cohortRows)
	for i := range overlap {
		overlap[i] = map[string]any{"cohort": fmt.Sprintf("daily-%d", i), "window": "30d", "product_a": 1, "product_b": 2, "shared_ips": 1, "ips_a": 2, "ips_b": 3}
	}
	fleet := doc["fleet"].([]any)
	all := map[string]any{"cohort": "*", "window": "30d", "products": 2, "providers": 2, "unique_ips": 999}
	kind := map[string]any{"cohort": "*:residential", "window": "30d", "products": 2, "providers": 2, "unique_ips": 923}
	doc["overlap"], doc["fleet"], doc["schema_version"] = overlap, append(fleet, all, kind), 4
	delete(doc, "products")
	delete(doc, "runs")
	head, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return head
}

// TestAV4BuildServesItsNetworks: a v4 build is read from its head and its
// network lists, each network served as one group with the summary's
// figures. On main it is read as a payload without products: nothing is
// served (regression demonstration, API-SUM-1 case 6).
func TestAV4BuildServesItsNetworks(t *testing.T) {
	t.Parallel()
	db, _ := networkLists(t)
	db.Publish(t, v4Head(t, 0))
	res, err := build.Build(load(t, db))
	if err != nil {
		t.Fatal(err)
	}
	ans := res.Pages.Network["shifter/residential"]
	if ans == nil || res.Details["acme"] == nil {
		t.Fatalf("the v4 build serves no shifter or acme network (%d details)", len(res.Details))
	}
	if got := ans.Ranges["30d"].Requests; got != 5000 {
		t.Errorf("shifter's 30d requests are %v, want the summary's 5000", got)
	}
	if n := ans.Ranges["30d"].Lists["city"]; n != 2 {
		t.Errorf("shifter's 30d city list has %d rows, want its network list's 2", n)
	}
}

// TestAV4HeadIsReadWithoutItsCohortRows is case 3 (API-SUM-1): of a v4 head
// with 10,000 per-cohort overlap rows none is read, nor a per-cohort fleet
// row; the every-group fleet entries are, and the networks' peers reach the
// ownership answer. The kind's entry is /v1/overlap's distinct count (before
// OVERLAP-DISTINCT-2 the head dropped it, and the market fell back to the sum
// of pools minus pairs).
func TestAV4HeadIsReadWithoutItsCohortRows(t *testing.T) {
	t.Parallel()
	db, _ := networkLists(t)
	db.Publish(t, v4Head(t, 10000))
	in := load(t, db)
	var head struct {
		Overlap []json.RawMessage `json:"overlap"`
		Fleet   []struct {
			Cohort string `json:"cohort"`
		} `json:"fleet"`
	}
	if err := json.Unmarshal(in.Snapshot.Payload, &head); err != nil {
		t.Fatal(err)
	}
	if head.Overlap != nil || len(head.Fleet) != 2 || head.Fleet[0].Cohort != "*" || head.Fleet[1].Cohort != "*:residential" ||
		bytes.Contains(in.Snapshot.Payload, []byte("daily-")) {
		t.Errorf("the head read has %d overlap rows and fleet %+v; want none and the \"*\" and \"*:residential\" entries only", len(head.Overlap), head.Fleet)
	}
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	if a, _ := res.Pages.Registered["/v1/overlap/residential/30d"].(*build.OverlapAnswer); a == nil || a.Market.DistinctIps != 923 ||
		a.Market.Basis == nil || *a.Market.Basis != build.BasisKindFleet {
		t.Errorf("the 30d overlap answer is %+v, want the kind entry's 923 distinct IPs", a)
	}
	detail, err := json.Marshal(res.Details["shifter"])
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(detail, []byte(`"overlaps":[{"slug":"acme","name":"Net acme","sharedIps":77,"shareOfOurs":0.11,"shareOfTheirs":0.2566`)) {
		t.Errorf("shifter's ownership answer lacks its acme peer:\n%.2000s", detail[max(bytes.Index(detail, []byte(`"overlaps"`)), 0):])
	}
}

// A head with networks and no schema version is not a v4 head: it is
// ErrBeforeV4 (review N1: the v4 test is never NULL).
func TestAHeadWithNetworksWithoutASchemaVersionIsBeforeV4(t *testing.T) {
	t.Parallel()
	db, _ := networkLists(t)
	var doc map[string]any
	if err := json.Unmarshal(v4Head(t, 0), &doc); err != nil {
		t.Fatal(err)
	}
	delete(doc, "schema_version")
	head, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	db.Publish(t, head)
	pool, err := store.OpenReadOnly(context.Background(), db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	if _, err := store.Load(context.Background(), pool); !errors.Is(err, store.ErrBeforeV4) {
		t.Errorf("a head without a schema version: %v, want ErrBeforeV4", err)
	}
}
