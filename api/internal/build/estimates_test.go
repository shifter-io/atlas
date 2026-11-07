package build

import (
	"encoding/json"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// estimateProduct is a product group whose 24h and 30d windows have the hour
// formula's figures (window 250.5; scale 2.5 in 24h, null in 30d; two
// released hours on the last day), whose "14d" window has them with scale 2
// and whose 7d window has none.
const estimateProduct = `{"id":"1:x","provider":"x","kind":"residential","windows":{
"24h":{"period":{"start":"2026-09-25T14:25:00+00:00","end":"2026-09-26T14:25:00+00:00","hours":24},
 "pool":{"unique_ips":100,"countries_breakdown":[{"country_code":"US","ips":61}],"geo":[{"ips":3}],"asns_breakdown":[{"asn":1,"ips":null}],"daily":{"unique_ips":10,"days":1}},
 "breakdowns":{"country":[{"country_code":"US","ips":61}],"city":[{"city":"A","ips":1}]},
 "series":[{"at":"2026-09-25T14:25:00+00:00","completed":5,"unique_ips":7},{"at":"2026-09-25T14:30:00+00:00","completed":5}],
 "estimated":{"estimated":true,"unique_ips":{"value":250.5,"estimated":true},"breakdown_scale":2.5,
  "series":[{"start":"2026-09-26 13:00:00+00:00","unique_ips":20.5},{"start":"2026-09-26T12:00:00+00:00","unique_ips":10.2},{"start":"bad","unique_ips":1}]}},
"30d":{"period":{"start":"2026-08-27T14:25:00+00:00","end":"2026-09-26T14:25:00+00:00","hours":720},
 "pool":{"unique_ips":100},
 "series":[{"at":"2026-09-24T14:25:00+00:00","unique_ips":40},{"at":"2026-09-25T14:25:00+00:00","unique_ips":50},{"at":"bad","unique_ips":9}],
 "estimated":{"unique_ips":{"value":250.5},"breakdown_scale":null,
  "series":[{"start":"2026-09-26T12:00:00+00:00","unique_ips":10.2},{"start":"2026-09-26T13:00:00+00:00","unique_ips":20.5}]}},
"14d":{"pool":{"unique_ips":100,"daily":null},"series":[{"at":"2026-09-25T14:25:00+00:00","unique_ips":7.25},{"at":"2026-09-26T14:25:00+00:00"}],
 "estimated":{"unique_ips":{"value":200},"breakdown_scale":2,"series":[]}},
"7d":{"pool":{"unique_ips":100},"series":[{"at":"2026-09-25T14:25:00+00:00","unique_ips":50}]}}}`

func decodeProduct(t *testing.T, body string) *snap.Product {
	t.Helper()
	var p snap.Product
	if err := json.Unmarshal([]byte(body), &p); err != nil {
		t.Fatal(err)
	}
	return &p
}

func TestTheMappingTakesTheFormulasFigures(t *testing.T) {
	t.Parallel()
	p := decodeProduct(t, estimateProduct)
	day, month, week := p.Window("24h"), p.Window("30d"), p.Window("7d")
	if ipsOf(day) != 251 || ipsOf(week) != 100 {
		t.Errorf("ipsOf: %v with figures, %v without", ipsOf(day), ipsOf(week))
	}
	// Half up, a null row and a window without a scale stay measured.
	if scaledIps(day, 61) != 153 || scaledIps(day, 3) != 8 || scaledIps(day, jsnum.NaN).OK() || scaledIps(month, 61) != 61 || scaledIps(week, 61) != 61 {
		t.Error("scaledIps")
	}
	if ipsPhrase(day) != " unique exit IPs" || ipsPhrase(week) != " distinct exit IPs observed" {
		t.Error("ipsPhrase")
	}
	month.Estimated.UniqueIps.Value = jsnum.NaN
	if ipsOf(month) != 100 {
		t.Error("a window figure of null keeps the measured IPs")
	}
	age := func(at string) Num { return Num(len(at)) }
	// 24h: one point per released hour, oldest first; the unparseable one is dropped.
	pts := ipPoints(day, "24h", day.Series, age)
	if len(pts) != 2 || pts[0].V != 10 || pts[1].V != 21 || pts[0].T != Num(len("2026-09-26T12:00:00+00:00")) {
		t.Errorf("24h points %+v", pts)
	}
	// A daily row is its measured IPs scaled half up like a place row, never
	// the released hours of its day added up (review B1: those would give 31
	// on the second day); a window without a scale stays measured.
	fortnight := p.Window("14d")
	pts = ipPoints(fortnight, "14d", fortnight.Series, age)
	if len(pts) != 2 || pts[0].V != 15 || pts[1].V.OK() {
		t.Errorf("14d points %+v", pts)
	}
	pts = ipPoints(day, "7d", day.Series, age)
	if len(pts) != 2 || pts[0].V != 18 || pts[1].V.OK() {
		t.Errorf("daily points of a scaled window %+v", pts)
	}
	pts = ipPoints(month, "30d", month.Series, age)
	if len(pts) != 3 || pts[0].V != 40 || pts[1].V != 50 || pts[2].V != 9 {
		t.Errorf("30d points %+v", pts)
	}
	if pts := ipPoints(week, "24h", week.Series, age); len(pts) != 1 || pts[0].V != 50 {
		t.Errorf("24h without figures %+v", pts)
	}
	if pts := ipPoints(week, "7d", week.Series, age); len(pts) != 1 || pts[0].V != 50 {
		t.Errorf("7d without figures %+v", pts)
	}
}
