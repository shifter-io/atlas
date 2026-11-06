package store_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
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
// and an every-group ("*") fleet entry.
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
	doc["overlap"], doc["fleet"], doc["schema_version"] = overlap, append(fleet, all), 4
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
// row; the every-group fleet entry is, and the networks' peers reach the
// ownership answer.
func TestAV4HeadIsReadWithoutItsCohortRows(t *testing.T) {
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
	if head.Overlap != nil || len(head.Fleet) != 1 || head.Fleet[0].Cohort != "*" || bytes.Contains(in.Snapshot.Payload, []byte("daily-")) {
		t.Errorf("the head read has %d overlap rows and fleet %+v; want none and the \"*\" entry only", len(head.Overlap), head.Fleet)
	}
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	detail, err := json.Marshal(res.Details["shifter"])
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(detail, []byte(`"overlaps":[{"slug":"acme","name":"Net acme","sharedIps":77,"shareOfOurs":0.11,"shareOfTheirs":0.2566`)) {
		t.Errorf("shifter's ownership answer lacks its acme peer:\n%.2000s", detail[max(bytes.Index(detail, []byte(`"overlaps"`)), 0):])
	}
}

// A head with networks and no schema version is not a v4 head: it loads as
// a payload before v4 (review N1: the v4 test is never NULL).
func TestAHeadWithNetworksWithoutASchemaVersionLoads(t *testing.T) {
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
	// A v4 read gives the build a payload with the networks as product groups.
	if in := load(t, db); in.Snapshot == nil || !bytes.Equal(in.Snapshot.Full(), in.Snapshot.Payload) {
		t.Error("a head without a schema version was read as v4")
	}
}
