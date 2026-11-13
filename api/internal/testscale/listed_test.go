package testscale

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/config"
	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// Listed gives the v1 fixture's windows, which have no breakdowns, their
// pool's country and ASN lists as breakdowns (IPs only), the row counts, and
// pool lists of the rows with IPs, most first; an ASN without a number stays
// one without.
func TestListedMakesTheListsOfOneSource(t *testing.T) {
	raw, err := os.ReadFile(config.FindUp("fixtures/measured/snapshot.json"))
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	w := doc["products"].([]any)[0].(map[string]any)["windows"].(map[string]any)["30d"].(map[string]any)
	asns := w["pool"].(map[string]any)["asns_breakdown"].([]any)
	asns[0].(map[string]any)["asn"], asns[1].(map[string]any)["ips"] = nil, 0
	if raw, err = json.Marshal(doc); err != nil {
		t.Fatal(err)
	}
	b, err := Listed(raw)
	if err != nil {
		t.Fatal(err)
	}
	var s snap.Snapshot
	if err := json.Unmarshal(b, &s); err != nil {
		t.Fatal(err)
	}
	got := s.Products[0].Windows["30d"]
	countries, asn := got.Breakdowns["country"], got.Breakdowns["asn"]
	if len(countries) != 3 || *countries[0].CountryCode != "US" || countries[0].Ips != 4960 || countries[0].Completed.OK() || countries[0].SuccessRate.OK() ||
		got.BreakdownEntries["country"] != 3 || got.BreakdownEntries["city"] != 0 || len(got.Breakdowns["city"]) != 0 {
		t.Errorf("country breakdown %+v, entries %v", countries, got.BreakdownEntries)
	}
	if len(asn) != len(asns) || asn[0].Asn.OK() || *asn[0].Name != "Comcast Cable Communications, LLC" || got.BreakdownEntries["asn"] != len(asns) {
		t.Errorf("ASN breakdown %+v", asn[:2])
	}
	pool := got.Pool.AsnsBreakdown
	if len(pool) != len(asns)-1 || pool[0].Ips != 1296 || pool[1].Ips != asn[2].Ips || pool[1].Ips < pool[2].Ips || len(got.Pool.CountriesBreakdown) != 3 {
		t.Errorf("pool ASNs %+v", pool[:3])
	}
	if _, err := Listed([]byte("{")); err == nil {
		t.Error("Listed of a broken payload: no error")
	}
}

// A row with requests gets its success rate; one without keeps it null.
func TestBreakdownRowSuccessRate(t *testing.T) {
	for _, c := range []struct {
		completed, successes int64
		want                 any
	}{{4, 3, 0.75}, {0, 0, nil}, {-1, -1, nil}} {
		r := BreakdownRows(map[string][]store.NetRow{"asn": {{Ips: 1, Completed: c.completed, Successes: c.successes, P50: -1}}})["asn"][0]
		if r["success_rate"] != c.want || r["p50"] != nil {
			t.Errorf("%d of %d: %+v", c.successes, c.completed, r)
		}
	}
}

// AsV4Answers gives each group's id and cohort as its network's and the
// snapshot's schema version as 4; groups that are not JSON objects fail.
func TestAsV4Answers(t *testing.T) {
	groups := [][]byte{[]byte(`{"id": "1:c0", "provider": "acme", "kind": "residential", "cohort": "c0"}`)}
	got, err := AsV4Answers(map[string][]byte{"a": []byte(`{"id":"1:c0","cohort":"c0","schemaVersion":2}`)}, groups)
	if err != nil || string(got["a"]) != `{"id":"acme:residential:network","cohort":"network","schemaVersion":4}` {
		t.Errorf("%s (%v)", got["a"], err)
	}
	if _, err := AsV4Answers(nil, [][]byte{[]byte("[]")}); err == nil {
		t.Error("a group that is not an object: no error")
	}
}
