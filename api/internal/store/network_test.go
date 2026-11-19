package store

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/shifter-io/atlas/api/internal/snap"
)

func ptr(s string) *string { return &s }

// netHead is a v4 head of two networks: a (product 1, one requested
// country) whose 30d window names list 11 and peers b, and whose 24h window
// names list 99, which is not loaded; b (product 2, two requested countries)
// whose 30d window names list 12 and peers a (the same pair) and a peer
// without a product id. Its per-cohort overlap row is replaced.
const netHead = `{"as_of": "2026-11-05T00:00:00+00:00", "overlap": [{"cohort": "c", "window": "30d", "product_a": 7, "product_b": 8}],
 "networks": [
  {"id": "a:residential:network", "product_id": 1, "provider": "a", "kind": "residential", "cohort": "network",
   "criteria": {"country": null, "requested_countries": ["DE"], "groups": 1},
   "windows": {"24h": {"period": {"hours": 24}, "lists": {"id": 99}},
    "30d": {"period": {"hours": 720}, "pool": {"unique_ips": 9, "list_entries": {"geo": 1}},
     "overlap": {"exclusive_share": 0.5, "peers": [{"provider": "b", "product_id": 2, "shared_ips": 5, "ips_ours": 9, "ips_theirs": 7}]},
     "lists": {"id": 11, "entries": {}}}}},
  {"id": "b:residential:network", "product_id": 2, "provider": "b", "kind": "residential", "cohort": "network",
   "criteria": {"country": null, "requested_countries": ["DE", "US"], "groups": 2},
   "windows": {"30d": {"period": {"hours": 720}, "lists": {"id": 12},
    "overlap": {"peers": [{"provider": "a", "product_id": 1, "shared_ips": 5, "ips_ours": 7, "ips_theirs": 9},
     {"provider": "x", "product_id": null, "shared_ips": 1}]}}}}],
 "schema_version": 4}`

// netList11 has two countries, a region, a city, 60 ASNs with IPs and one
// without, and two City x ASN rows naming the same place.
func netList11(t *testing.T) *NetList {
	t.Helper()
	de := &Place{CountryCode: ptr("DE"), Country: ptr("Germany")}
	us := &Place{CountryCode: ptr("US"), Country: ptr("United States")}
	berlin := &Place{CountryCode: ptr("DE"), Country: ptr("Germany"), RegionCode: ptr("BE"), Region: ptr("Berlin"), City: ptr("Berlin")}
	rows := map[string][]NetRow{
		"country":  {{Place: de, Ips: 5, Completed: 10, Successes: 9, P50: 100}, {Place: us, Ips: 8, Completed: -1, Successes: -1, P50: -1}},
		"region":   {{Place: &Place{CountryCode: ptr("DE"), Region: ptr("Berlin")}, Ips: 5, Completed: 10, Successes: 9, P50: 100}},
		"city":     {{Place: berlin, Ips: 5, Completed: 10, Successes: 9, P50: 100}},
		"city_asn": {{Place: berlin, Network: &Network{Name: ptr("One")}, Ips: 3}, {Place: berlin, Network: &Network{Name: ptr("Two")}, Ips: 2}},
	}
	for i := range 61 {
		asn := int64(64512 + i)
		rows["asn"] = append(rows["asn"], NetRow{Network: &Network{Asn: &asn, Name: ptr(fmt.Sprint("AS ", i))}, Ips: int64(max(60-i, 0)), Completed: 1, Successes: 1})
	}
	l, err := NewNetList(rows)
	if err != nil {
		t.Fatal(err)
	}
	return l
}

func netSnapshot(t *testing.T) (*SnapshotRow, *snap.Snapshot) {
	t.Helper()
	l12, err := NewNetList(map[string][]NetRow{"country": {{Place: &Place{CountryCode: ptr("US")}, Ips: 1}}})
	if err != nil {
		t.Fatal(err)
	}
	s := &SnapshotRow{BuildID: 3, Payload: []byte(netHead)}
	if err := s.SetNetworks(map[int64]*NetList{11: netList11(t), 12: l12}); err != nil {
		t.Fatal(err)
	}
	var doc snap.Snapshot
	if err := json.Unmarshal(s.Full(), &doc); err != nil {
		t.Fatalf("%v\n%s", err, s.Full())
	}
	return s, &doc
}

func TestSetNetworksGivesEachNetworkAsOneGroup(t *testing.T) {
	t.Parallel()
	s, doc := netSnapshot(t)
	if string(s.Payload) != netHead || !bytes.Contains(s.Full(), []byte(`"products": [{"id": "a:residential:network"`)) {
		t.Errorf("the head changed, or the payload the build reads has no products:\n%.300s", s.Full())
	}
	if len(doc.Products) != 2 || doc.Products[0].ID != "a:residential:network" || doc.Products[1].Cohort != NetworkCohort {
		t.Fatalf("groups %+v", doc.Products)
	}
	a, b := &doc.Products[0], &doc.Products[1]
	if a.Criteria.Country == nil || *a.Criteria.Country != "DE" || b.Criteria.Country != nil || len(b.Criteria.RequestedCountries) != 2 || b.Criteria.Groups != 2 {
		t.Errorf("criteria %+v, %+v", a.Criteria, b.Criteria)
	}
	w := a.Windows["30d"]
	if bd := w.Breakdowns; len(bd["country"]) != 2 || bd["city_asn"] != nil || bd["region"] != nil || bd["country"][0].SuccessRate != 0.9 ||
		!math.IsNaN(bd["country"][1].Completed.F()) || bd["country"][0].P90.OK() {
		t.Errorf("breakdowns %+v", bd)
	}
	if e := w.BreakdownEntries; fmt.Sprint(e) != "map[asn:61 city:1 city_asn:2 country:2 region:1]" {
		t.Errorf("breakdown_entries %v", e)
	}
	asns := w.Pool.AsnsBreakdown
	if len(asns) != 60 || asns[0].Ips != 60 || asns[59].Ips != 1 || w.Pool.UniqueIps != 9 {
		t.Errorf("pool ASNs %d rows (first %+v), unique IPs %v", len(asns), asns[0], w.Pool.UniqueIps)
	}
	if c := w.Pool.CountriesBreakdown; len(c) != 2 || c[0].CountryCode != "US" || c[1].Ips != 5 {
		t.Errorf("pool countries %+v", c)
	}
	if w.Overlap.ExclusiveShare != 0.5 || len(w.Overlap.Peers) != 1 {
		t.Errorf("overlap %+v", w.Overlap)
	}
	if w24 := a.Windows["24h"]; w24.Breakdowns != nil {
		t.Errorf("a window naming a list not loaded got breakdowns %+v", w24.Breakdowns)
	}
	want := snap.OverlapRow{Cohort: NetworkCohort, Window: "30d", ProductA: 1, ProductB: 2, SharedIps: 5, IpsA: 9, IpsB: 7}
	if len(doc.Overlap) != 1 || doc.Overlap[0] != want {
		t.Errorf("overlap rows %+v, want only %+v", doc.Overlap, want)
	}
}

// A network window's list is the snapshot's compact list, as loaded; a
// window naming a list not loaded has none.
func TestSetNetworksKeepsTheCompactLists(t *testing.T) {
	t.Parallel()
	s, _ := netSnapshot(t)
	if len(s.Networks) != 2 || s.Networks[PlaceWindow{1, NetworkCohort, 24}] != nil || s.Places != nil {
		t.Fatalf("networks %v, places %v", s.Networks, s.Places)
	}
	l := s.Networks[PlaceWindow{1, NetworkCohort, 720}]
	if l.Len("city_asn") != 2 || dim(t, l, "city_asn").Row(0).Place != dim(t, l, "city").Row(0).Place || s.Networks[PlaceWindow{2, NetworkCohort, 720}].Len("asn") != 0 {
		t.Errorf("list 11 %+v", l)
	}
	head, err := l.headRows(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if c := head.Rows("country")[1]; c.Ips != 8 || c.Completed != -1 || c.Observations != -1 || c.P90 != -1 || c.P99 != -1 || c.Challenges != -1 ||
		c.Blocks != -1 || head.Rows("city") != nil || len(head.Rows("asn")) != 61 {
		t.Errorf("head rows: country %+v, %d ASN rows", c, len(head.Rows("asn")))
	}
}

func TestSetNetworksRefusesAHeadWithoutNetworks(t *testing.T) {
	t.Parallel()
	for _, head := range []string{`{"schema_version": 4}`, `{"networks": {}}`, `{"networks": [1]}`, `[]`, `{"networks": [null]}`,
		`{"networks": [{"product_id": 1, "windows": {"30d": {"lists": {"id": 1}, "pool": 3}}}]}`,
		`{"networks": [{"product_id": 1, "criteria": {"requested_countries": ["DE"]}, "windows": 2}]}`,
		`{"networks": [{"product_id": 1, "windows": null}]}`, `{"products": [], "networks": []}`} {
		s := &SnapshotRow{Payload: []byte(head)}
		l, _ := NewNetList(nil)
		if err := s.SetNetworks(map[int64]*NetList{1: l}); err == nil {
			t.Errorf("%s: no error", head)
		}
	}
	s := &SnapshotRow{Payload: []byte(netHead)}
	err := s.setNetworks(context.Background(), nil, func(ids []int64) (map[int64]*NetList, error) { return nil, fmt.Errorf("no lists %v", ids) })
	if err == nil || s.full != nil {
		t.Errorf("a failed list load gave %v, a payload of %d bytes", err, len(s.full))
	}
	// A network without windows is served as it is.
	s = &SnapshotRow{Payload: []byte(`{"networks": [{"product_id": 1, "criteria": {"requested_countries": ["DE"]}}]}`)}
	if err := s.SetNetworks(nil); err != nil || !bytes.Contains(s.Full(), []byte(`"criteria": {"country": "DE"`)) {
		t.Errorf("%v: %s", err, s.Full())
	}
}

func TestNewNetListRefusesWhatAListCannotHold(t *testing.T) {
	t.Parallel()
	for name, rows := range map[string][]NetRow{"country_asn": {{}}, "nonsense": {{}}, "asn": {{Ips: 1 << 32}}} {
		if _, err := NewNetList(map[string][]NetRow{name: rows}); err == nil {
			t.Errorf("%s: no error", name)
		}
	}
}

// A v4 read drops the group lists a cache holds.
func TestListsHoldOneKindOfList(t *testing.T) {
	t.Parallel()
	c := NewLists()
	c.held = map[int64]*List{1: {}}
	n := c.networkLists(nil)
	if c.held != nil || c.networkLists(nil) != n {
		t.Error("the network lists did not drop the group lists, or were made twice")
	}
	if other := c.networkLists(&pgx.Conn{}); other == n || other.db == nil {
		t.Error("network lists read through another database were kept")
	}
}
