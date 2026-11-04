package build_test

import (
	"bytes"
	"encoding/json"
	"math"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index-website/api/internal/build"
	"github.com/Zeus-Development/proxy-index-website/api/internal/server"
	"github.com/Zeus-Development/proxy-index-website/api/internal/testdb"
)

// The hour formula's figures (proxy-index estimates/window.py, contract K8)
// on the every-request fixture: every window of the shifter group gets an
// `estimated` block (window figure 12417.5, breakdown scale 2.5, released
// hours from 2026-09-24T15:00 to the last hour ended before as_of), and a
// second network, rival, has more measured IPs and no block.
const (
	estimateValue = 12417.5 // rounds half up to 12418
	estimateScale = 2.5
	rivalIps      = 6000
)

var estimateAsOf = time.Date(2026, 9, 26, 14, 25, 0, 0, time.UTC)

// estimateHours is the released hours inside a window starting at start:
// one per UTC hour from 2026-09-24T15:00, each ending by as_of, with
// unique_ips 100.5 + i (so each rounds half up).
func estimateHours(start time.Time) []map[string]any {
	out := []map[string]any{}
	for i, h := 0, time.Date(2026, 9, 24, 15, 0, 0, 0, time.UTC); !h.Add(time.Hour).After(estimateAsOf); i, h = i+1, h.Add(time.Hour) {
		if !h.Before(start) {
			out = append(out, map[string]any{"start": h.Format("2006-01-02T15:04:05+00:00"), "unique_ips": 100.5 + float64(i),
				"requests": 50, "estimated": true, "formula_version": 1})
		}
	}
	return out
}

func estimatedSnapshot(t *testing.T) []byte {
	t.Helper()
	body, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil {
		t.Fatal(err)
	}
	var s map[string]any
	if err := json.Unmarshal(body, &s); err != nil {
		t.Fatal(err)
	}
	products := s["products"].([]any)
	shifter := products[0].(map[string]any)
	for _, w := range shifter["windows"].(map[string]any) {
		w := w.(map[string]any)
		start, _ := time.Parse(time.RFC3339, w["period"].(map[string]any)["start"].(string))
		w["estimated"] = map[string]any{"estimated": true, "formula_versions": []int{1}, "hours": 3,
			"unique_ips": map[string]any{"value": estimateValue, "estimated": true, "formula_version": 1, "method": "distinct"},
			"requests":   map[string]any{"value": 900, "estimated": true, "formula_version": 1},
			"countries": []any{map[string]any{"country_code": "US", "unique_ips": estimateValue, "requests": 900,
				"estimated": true, "formula_version": 1}},
			"series": estimateHours(start), "breakdown_scale": estimateScale,
			"carried": map[string]any{"shared_share": nil, "datacenter_asn_share": 0.01, "latency": "measured"}}
	}
	var rival map[string]any
	copied, _ := json.Marshal(shifter)
	if err := json.Unmarshal(copied, &rival); err != nil {
		t.Fatal(err)
	}
	rival["id"], rival["provider"], rival["product_id"], rival["name"] = "2:rival", "rival", 2, "Rival"
	for _, w := range rival["windows"].(map[string]any) {
		w := w.(map[string]any)
		delete(w, "estimated")
		w["pool"].(map[string]any)["unique_ips"] = rivalIps
	}
	s["products"] = append(products, rival)
	out, err := json.Marshal(s)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func estimatedBuild(t *testing.T) *build.Result {
	t.Helper()
	db := testdb.New(t)
	db.Publish(t, estimatedSnapshot(t))
	return buildFrom(t, db)
}

func half(v float64) Num { return Num(math.Floor(v + 0.5)) }

// Every IP value the API derives from a window with the formula's figures
// follows K8: the window figure, measured rows times the breakdown scale and
// the released hours as the chart.
func TestTheHourFormulaGivesTheIPNumbers(t *testing.T) {
	res := estimatedBuild(t)
	want := half(estimateValue)
	d := res.Details["shifter"]
	p := d.Provider
	if p.Pool.UniqueIps24h != want || p.Pool.UniqueIps30d != want {
		t.Errorf("provider pool %v / %v, want %v", p.Pool.UniqueIps24h, p.Pool.UniqueIps30d, want)
	}
	for _, g := range p.Geo {
		measured := map[string]float64{"US": 4960, "FI": 6, "JP": 1}[g.CountryCode]
		if g.Ips != half(measured*estimateScale) || g.Share != g.Ips/want {
			t.Errorf("geo %s: %v ips, share %v", g.CountryCode, g.Ips, g.Share)
		}
	}
	if len(p.Asns) == 0 || p.Asns[0].Ips != half(1296*estimateScale) {
		t.Errorf("first ASN bucket %+v", p.Asns)
	}
	if !strings.Contains(strings.Join(p.Strengths, "|"), "12,418 unique exit IPs") {
		t.Errorf("strengths %v", p.Strengths)
	}
	ans := res.Pages.Network["shifter/residential"]
	for _, r := range build.Ranges {
		tot := ans.Ranges[r.ID]
		if tot.UniqueIps != want || tot.IpsSum != want {
			t.Errorf("%s totals %v / %v, want %v", r.ID, tot.UniqueIps, tot.IpsSum, want)
		}
		if len(tot.Countries) != 2 || tot.Countries[0].IpsSum != half(4000*estimateScale) || tot.Countries[1].IpsSum != half(900*estimateScale) {
			t.Errorf("%s country rows %+v", r.ID, tot.Countries)
		}
		if p.TileRanks["residential"][r.ID].UniqueIps.Rank != 1 {
			t.Errorf("%s: shifter's IP tile rank %+v, want first ahead of rival's measured %d", r.ID, p.TileRanks["residential"][r.ID].UniqueIps, rivalIps)
		}
		if d.Windows["residential"][r.ID].UniqueIps != want {
			t.Errorf("%s window stats %v", r.ID, d.Windows["residential"][r.ID].UniqueIps)
		}
	}
	us := res.Pages.Country["US"].Ranges["30d"]
	for _, row := range us {
		if row.Network == "shifter" && row.IpsSum != half(4000*estimateScale) {
			t.Errorf("country answer row %+v", row)
		}
	}
	checkEstimatedCharts(t, res)
}

func checkEstimatedCharts(t *testing.T, res *build.Result) {
	t.Helper()
	day := estimateHours(estimateAsOf.Add(-24 * time.Hour))
	chart := res.Pages.Charts["shifter/residential/24h"].Pool
	if len(chart) != len(day) {
		t.Fatalf("24h IP chart has %d points, want one per released hour (%d)", len(chart), len(day))
	}
	for i, h := range day {
		at, _ := time.Parse(time.RFC3339, h["start"].(string))
		if chart[i].V != half(h["unique_ips"].(float64)) || chart[i].T != Num(estimateAsOf.Sub(at).Minutes()) {
			t.Errorf("24h point %d = %+v, hour %v", i, chart[i], h)
		}
	}
	if pts := res.Details["shifter"].Provider.Series.UniqueIps24h; len(pts) != len(day) || pts[0].V != half(day[0]["unique_ips"].(float64)) {
		t.Errorf("provider 24h IP series %v", pts)
	}
	// A daily point is the measured day's IPs times the breakdown scale, half
	// up, like a place row; the released hours of that day are not added up
	// (review B1). Days measured as null stay null.
	measured := measuredDays(t)
	month := res.Pages.Charts["shifter/residential/30d"].Pool
	if len(month) != len(measured) || month[len(month)-1].V != half(4967*estimateScale) {
		t.Errorf("30d IP chart %v, want %d points ending at %v", month, len(measured), half(4967*estimateScale))
	}
	for i, m := range measured {
		if want := scaledOrNull(m.ips); i < len(month) && !sameNum(month[i].V, want) {
			t.Errorf("30d point %d = %v, want %v", i, month[i].V, want)
		}
	}
	scaled := 0
	for _, d := range res.Pages.Network["shifter/residential"].Days {
		for _, m := range measured {
			if m.day == d.Day && m.ips.OK() {
				scaled++
				if want := scaledOrNull(m.ips); d.UniqueIps != want || d.IpsSum != want {
					t.Errorf("day %s: %v / %v, want %v", d.Day, d.UniqueIps, d.IpsSum, want)
				}
			}
		}
	}
	if scaled == 0 {
		t.Error("no measured day to check")
	}
	rival := res.Pages.Network["rival/residential"]
	if rival.Ranges["30d"].UniqueIps != rivalIps || rival.Ranges["30d"].Countries[0].IpsSum != 4000 {
		t.Errorf("rival (no figures) is not measured: %+v", rival.Ranges["30d"])
	}
}

type measuredDay struct {
	day string
	ips Num
}

// measuredDays is the fixture's measured 30d series (the shifter group).
func measuredDays(t *testing.T) []measuredDay {
	t.Helper()
	var s struct {
		Products []struct {
			Windows map[string]struct {
				Series []struct {
					At        string   `json:"at"`
					UniqueIps *float64 `json:"unique_ips"`
				} `json:"series"`
			} `json:"windows"`
		} `json:"products"`
	}
	if err := json.Unmarshal(estimatedSnapshot(t), &s); err != nil {
		t.Fatal(err)
	}
	out := []measuredDay{}
	for _, r := range s.Products[0].Windows["30d"].Series {
		ips := Num(math.NaN())
		if r.UniqueIps != nil {
			ips = Num(*r.UniqueIps)
		}
		out = append(out, measuredDay{day: r.At[:10], ips: ips})
	}
	return out
}

func scaledOrNull(ips Num) Num {
	if !ips.OK() {
		return ips
	}
	return half(ips.F() * estimateScale)
}

func sameNum(a, b Num) bool { return a == b || (!a.OK() && !b.OK()) }

// No answer body names the estimate: no `estimated` key or flag and no
// "estimate" text, in every pre-rendered answer, the dataset and the pages.
// The tape keeps an hour's release row, worded as an hourly update (owner
// decision on review question 2), with no exception to the check.
func TestNoAnswerNamesTheEstimate(t *testing.T) {
	db := testdb.New(t)
	db.Publish(t, estimatedSnapshot(t))
	db.Exec(t, `INSERT INTO test_events(at,kind,product_id,country_code,signal,severity,data) VALUES
 ('2026-09-26T14:01:00+00','estimate_hour_released',(SELECT product_id FROM products WHERE provider='shifter'),'US',0,'info',
  '{"day": "2026-09-26", "hour": 13, "formula_version": 1, "unique_ips": 124, "countries": 1}')`)
	res := buildFrom(t, db)
	released := false
	for _, e := range res.Dataset.Tape {
		released = released || e.Title == "Hourly update for 13:00 UTC"
	}
	if !released {
		t.Fatalf("the tape has no hourly update row to check: %+v", res.Dataset.Tape)
	}
	v, err := server.RenderVersion(1, res, server.Options{})
	if err != nil {
		t.Fatal(err)
	}
	var dataset bytes.Buffer
	if err := build.WriteDataset(&dataset, res.Dataset); err != nil {
		t.Fatal(err)
	}
	bodies := map[string][]byte{"/v1/dataset": dataset.Bytes()}
	for path, r := range v.Static {
		bodies[path] = r.Identity
	}
	for _, scope := range []string{"shifter/residential", "country/US"} {
		dim := "country"
		if strings.HasPrefix(scope, "country/") {
			dim = "city"
		}
		page, ok := res.Pages.List(res.Dataset.GeneratedAt, scope, "30d", dim, "", 1)
		if !ok {
			t.Fatalf("no %s list", scope)
		}
		b, err := server.Marshal(page)
		if err != nil {
			t.Fatal(err)
		}
		bodies["/v1/places/"+scope] = b
	}
	if len(bodies) < 10 {
		t.Fatalf("only %d answers rendered", len(bodies))
	}
	for path, b := range bodies {
		if i := bytes.Index(bytes.ToLower(b), []byte("estimat")); i >= 0 {
			t.Errorf("%s names the estimate: …%s…", path, b[max(0, i-60):min(len(b), i+40)])
		}
	}
}
