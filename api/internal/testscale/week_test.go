package testscale

import (
	"encoding/json"
	"testing"
)

// The week's payload has 8 browser targets besides the AI group's 3
// sources, each with both engines' figures and scores, a measured value at
// every 5-minute point of its 24-hour series, and lists of the shape's sizes.
func TestWeekShape(t *testing.T) {
	week := Week
	week.Networks = 2
	payload, err := week.Payload()
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Products []struct {
			Windows map[string]struct {
				Targets map[string]struct {
					Browser *struct {
						Group   *string                         `json:"group"`
						Engines map[string]struct{ Scores any } `json:"engines"`
					} `json:"browser"`
				} `json:"targets"`
				Series []struct {
					UniqueIps *int `json:"unique_ips"`
				} `json:"series"`
				BrowserOverall map[string]any `json:"browser_overall"`
			} `json:"windows"`
		} `json:"products"`
	}
	if err := json.Unmarshal(payload, &doc); err != nil {
		t.Fatal(err)
	}
	if len(doc.Products) != 2 {
		t.Fatalf("%d product groups, want 2", len(doc.Products))
	}
	w := doc.Products[0].Windows["24h"]
	targets, sources := 0, 0
	for id, target := range w.Targets {
		switch b := target.Browser; {
		case b == nil:
		case len(b.Engines) != len(Engines) || b.Engines["chrome"].Scores == nil:
			t.Errorf("%s has engines %v", id, b.Engines)
		case b.Group != nil:
			sources++
		default:
			targets++
		}
	}
	if targets != 8 || sources != 3 || len(w.BrowserOverall) != len(Engines) {
		t.Errorf("%d browser targets, %d sources, overall %v; want 8, 3 and both engines", targets, sources, w.BrowserOverall)
	}
	if len(w.Series) != 288 || w.Series[287].UniqueIps == nil {
		t.Errorf("24h series of %d points, the last %+v", len(w.Series), w.Series[len(w.Series)-1])
	}
	lists := week.Lists()
	if got := RowCounts(lists); got["country"] != 249 || got["city"] != 30000 || got["asn"] != 9000 || got["city_asn"] != 30000 {
		t.Errorf("list rows %v", got)
	}
	if r := lists["city_asn"][9000]; r.Place == nil || r.Network == nil || *r.Network.Asn != 64512 {
		t.Errorf("City x ASN row 9000: %+v", r)
	}
}
