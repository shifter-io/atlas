package testscale

import (
	"encoding/json"
	"math"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// A small v3 build of two networks, one group each, with overlap rows,
// becomes their v4 head and lists: each network's own requested country,
// its peers from either side of a row (rows without shared IPs left out),
// and lists of the rows the build reads.
func TestNetworksOfASmallBuild(t *testing.T) {
	in, err := Inputs(Shape{Networks: 2, Countries: 1, Exits: 3, Tail: 4, CityAsn: 5, NoFleetAll: true})
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(in.Snapshot.Payload, &doc); err != nil {
		t.Fatal(err)
	}
	doc["overlap"] = []map[string]any{{"cohort": "daily-000", "window": "30d", "product_a": 1, "product_b": 1001, "shared_ips": 7, "ips_a": 10, "ips_b": 20},
		{"cohort": "daily-000", "window": "30d", "product_a": 1001, "product_b": 1, "shared_ips": 0}}
	if in.Snapshot.Payload, err = json.Marshal(doc); err != nil {
		t.Fatal(err)
	}
	head, lists, err := Networks(in.Snapshot, 0)
	if err != nil {
		t.Fatal(err)
	}
	var v4 struct {
		SchemaVersion int `json:"schema_version"`
		Networks      []struct {
			ID       string        `json:"id"`
			Criteria snap.Criteria `json:"criteria"`
			Windows  map[string]struct {
				Overlap snap.WindowOverlap `json:"overlap"`
				Lists   struct {
					ID      int64          `json:"id"`
					Entries map[string]int `json:"entries"`
				} `json:"lists"`
			} `json:"windows"`
		} `json:"networks"`
		Overlap any `json:"overlap"`
	}
	if err := json.Unmarshal(head, &v4); err != nil || v4.SchemaVersion != 4 || len(v4.Networks) != 2 || v4.Overlap != nil || len(lists) != 6 {
		t.Fatalf("head %+v, %d lists (%v)", v4, len(lists), err)
	}
	a, b := v4.Networks[0], v4.Networks[1]
	if a.ID != "net00:residential:network" || len(a.Criteria.RequestedCountries) != 1 || a.Criteria.RequestedCountries[0] != "AA" || a.Criteria.Groups != 1 {
		t.Errorf("network %s criteria %+v", a.ID, a.Criteria)
	}
	pa, pb := a.Windows["30d"].Overlap.Peers, b.Windows["30d"].Overlap.Peers
	if len(pa) != 1 || pa[0].ProductID != 1001 || pa[0].IpsOurs != 10 || len(pb) != 1 || pb[0].Provider != "net00" || pb[0].IpsOurs != 20 ||
		len(a.Windows["24h"].Overlap.Peers) != 0 {
		t.Errorf("peers %+v, %+v", pa, pb)
	}
	w := a.Windows["30d"].Lists
	l := lists[w.ID]
	if w.Entries["country"] != 3 || w.Entries["region"] != 4 || w.Entries["city_asn"] != 5 || l.Len("city_asn") != 5 || l.Len("asn") != 4 {
		t.Errorf("list %d: entries %v", w.ID, w.Entries)
	}
	if r := l.Row("city_asn", 0); r.Place == nil || r.Place.City == nil || r.Network == nil || r.Network.Asn == nil || r.Completed != 3000 {
		t.Errorf("city x ASN row %+v", r)
	}
	if count(snap.Num(math.NaN())) != -1 || count(2.6) != 3 {
		t.Error("count")
	}
}

// A window whose typed rows are not whole reads its region, city and ASN
// rows from its breakdowns.
func TestNetworksReadsCappedBreakdowns(t *testing.T) {
	in, err := Inputs(Shape{Networks: 1, Countries: 1, Exits: 2, Tail: 30})
	if err != nil {
		t.Fatal(err)
	}
	in.Snapshot.Places = nil
	_, lists, err := Networks(in.Snapshot, 3)
	if err != nil {
		t.Fatal(err)
	}
	for _, l := range lists {
		if l.Len("region") != 25 || l.Len("asn") != 25 || l.Row("asn", 0).Network.Asn == nil || l.Row("region", 0).Place.City != nil {
			t.Errorf("capped list: %d regions, %d ASNs", l.Len("region"), l.Len("asn"))
		}
	}
}
