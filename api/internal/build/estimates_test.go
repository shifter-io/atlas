package build

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index-website/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index-website/api/internal/snap"
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

func TestTheRawResultFollowsTheMapping(t *testing.T) {
	p := decodeProduct(t, estimateProduct)
	raw, err := measurementOf(p)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "estimat") {
		t.Fatalf("raw result names the estimate: %s", raw)
	}
	var got struct {
		Windows map[string]snap.Window `json:"windows"`
	}
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	day, month, week, fortnight := got.Windows["24h"], got.Windows["30d"], got.Windows["7d"], got.Windows["14d"]
	if day.Pool.UniqueIps != 251 || day.Pool.CountriesBreakdown[0].Ips != 153 || day.Pool.AsnsBreakdown[0].Ips.OK() {
		t.Errorf("24h pool %+v", day.Pool)
	}
	if day.Breakdowns["country"][0].Ips != 153 || day.Breakdowns["city"][0].Ips != 3 {
		t.Errorf("24h breakdowns %+v", day.Breakdowns)
	}
	if day.Series[0].UniqueIps.OK() || day.Series[1].UniqueIps.OK() || day.Series[0].Completed != 5 {
		t.Errorf("24h buckets keep a measured IP count: %+v", day.Series)
	}
	if month.Pool.UniqueIps != 251 || month.Series[0].UniqueIps != 40 || month.Series[1].UniqueIps != 50 || month.Series[2].UniqueIps != 9 {
		t.Errorf("30d %+v %+v", month.Pool, month.Series)
	}
	if fortnight.Pool.UniqueIps != 200 || fortnight.Series[0].UniqueIps != 15 || fortnight.Series[1].UniqueIps.OK() {
		t.Errorf("14d %+v %+v", fortnight.Pool, fortnight.Series)
	}
	if week.Pool.UniqueIps != 100 || week.Series[0].UniqueIps != 50 {
		t.Errorf("7d changed: %+v", week)
	}
	var geo struct {
		Windows map[string]struct {
			Pool struct {
				Geo []struct {
					Ips float64 `json:"ips"`
				} `json:"geo"`
				Daily *struct {
					UniqueIps float64 `json:"unique_ips"`
					Days      float64 `json:"days"`
				} `json:"daily"`
			} `json:"pool"`
		} `json:"windows"`
	}
	if err := json.Unmarshal(raw, &geo); err != nil || geo.Windows["24h"].Pool.Geo[0].Ips != 8 {
		t.Errorf("24h geo %+v %v", geo.Windows["24h"].Pool.Geo, err)
	}
	if d := geo.Windows["24h"].Pool.Daily; d == nil || d.UniqueIps != 25 || d.Days != 1 || geo.Windows["14d"].Pool.Daily != nil {
		t.Errorf("pool daily %+v / %+v", d, geo.Windows["14d"].Pool.Daily)
	}
	plain := decodeProduct(t, `{"id":"1:x","windows":{"24h":{"pool":{"unique_ips":1}}}}`)
	if raw, err := measurementOf(plain); err != nil || string(raw) != string(plain.Raw) {
		t.Errorf("a product without figures is not passed through: %s %v", raw, err)
	}
	for _, bad := range []string{`{"windows":{"24h":{"estimated":{}}}`, `{"windows":{"24h":{"estimated":{},"pool":[]}}}`,
		`{"windows":{"24h":{"estimated":{},"pool":{"geo":{}}}}}`, `{"windows":{"24h":{"estimated":{},"pool":{"geo":[{"ips":"x"}]}}}}`,
		`{"windows":{"24h":{"estimated":{},"breakdowns":[]}}}`, `{"windows":{"24h":{"estimated":{},"series":{}}}}`,
		`{"windows":{"24h":{"estimated":{},"pool":{"daily":[]}}}}`, `{"windows":{"24h":{"estimated":{},"pool":{"daily":{"unique_ips":"x"}}}}}`,
		`{"windows":{"30d":{"estimated":{},"series":[{"unique_ips":"x"}]}}}`} {
		p := decodeProduct(t, `{"windows":{"24h":{"estimated":{}},"30d":{"estimated":{}}}}`)
		p.Raw = []byte(bad)
		if _, err := measurementOf(p); err == nil {
			t.Errorf("%s: no error", bad)
		}
	}
}
