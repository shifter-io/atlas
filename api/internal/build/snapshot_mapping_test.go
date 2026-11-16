package build_test

import (
	"encoding/json"
	"math"
	"os"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/model"
)

// The committed fixture is a v1 snapshot without outages; this publishes it
// turned into v2 with outages and time-based uptime and checks the mapping.
func TestOutagesMapToIncidentsAndBands(t *testing.T) {
	t.Parallel()
	res := publishedBuild(t, withOutages(t))
	p := res.Details["shifter"].Provider
	if len(p.Incidents) != len(fixtureOutages) {
		t.Fatalf("incidents %d, want %d", len(p.Incidents), len(fixtureOutages))
	}
	us := p.Incidents[1]
	if us.StartedAgoSeconds != 6*3600+25*60 || us.DurationMinutes != 120 || model.SV(us.Scope) != "US" || us.Impact != 0.5 {
		t.Errorf("2-hour US outage mapped as %+v", us)
	}
	if p.Performance.DowntimeMinutes24h != 75 { // 9000 s over 2 countries
		t.Errorf("downtimeMinutes24h %v", p.Performance.DowntimeMinutes24h)
	}
	for _, c := range []struct {
		rangeID  string
		count    int
		downtime float64
	}{{"24h", 2, 75}, {"7d", 2, 75}, {"30d", 3, 135}} {
		w := res.Details["shifter"].Windows["residential"][c.rangeID]
		if len(w.Availability) != len(w.Success) || w.Availability[len(w.Availability)-1].V != 0.9995 || w.Availability[0].V.OK() {
			t.Errorf("%s: availability history lost measured fractions or null gaps", c.rangeID)
		}
		if len(w.Bands) != c.count || w.IncidentCount != Num(c.count) || w.DowntimeMinutes != Num(c.downtime) {
			t.Errorf("%s: %d bands, count %v, downtime %v", c.rangeID, len(w.Bands), w.IncidentCount, w.DowntimeMinutes)
		}
	}
	// The US outage sits 6h25m to 4h25m before the end of the 24h window.
	b := res.Details["shifter"].Windows["residential"]["24h"].Bands[1]
	if math.Abs(b.From.F()-(1-385.0/1440)) > 1e-12 || math.Abs(b.To.F()-(1-265.0/1440)) > 1e-12 || model.SV(b.Severity) != "major" || model.SV(b.Country) != "US" {
		t.Errorf("24h band %+v", b)
	}
}

// The framework's every-request snapshot shape (fixtures/measured/
// snapshot-every-request.json, scripts/every-request-fixture.mjs): latency
// under load, time-weighted success, per-IP shares and list prices.
func TestEveryRequestSnapshotsMapLatencyPricingAndCopy(t *testing.T) {
	t.Parallel()
	snapshot, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil {
		t.Fatal(err)
	}
	res := publishedBuild(t, snapshot)
	p := res.Details["shifter"].Provider
	w := res.Details["shifter"].Windows["residential"]["24h"]
	// The headline is the pulses' latency (HEADLINE-PULSE-1), the light-load
	// fields equal it, and the latency under load is served beside it.
	if !w.LightLatencyP50.OK() || w.LatencyP50 != w.LightLatencyP50 || !(w.UnderLoadLatencyP50 > w.LatencyP50) ||
		w.LatencyUnderLoad == nil || *w.LatencyUnderLoad {
		t.Errorf("headline %v should be light load %v, under load %v beside it (flag %v)", w.LatencyP50, w.LightLatencyP50,
			w.UnderLoadLatencyP50, w.LatencyUnderLoad)
	}
	// Latency is the HTTP target's only, never the window's HTTP + HTTPS mixture.
	var snapshotDoc struct {
		Products []struct {
			Windows map[string]struct {
				Targets map[string]struct {
					Metrics struct {
						LatencyMs map[string]float64 `json:"latency_ms"`
					} `json:"metrics"`
				} `json:"targets"`
			} `json:"windows"`
		} `json:"products"`
	}
	if err := json.Unmarshal(snapshot, &snapshotDoc); err != nil {
		t.Fatal(err)
	}
	httpLight := snapshotDoc.Products[0].Windows["24h"].Targets["generic_http"].Metrics.LatencyMs["p50"]
	if w.LatencyTarget == nil || *w.LatencyTarget != "generic_http" || w.LightLatencyP50.F() != httpLight {
		t.Errorf("latency target %v, light p50 %v, want generic_http %v", w.LatencyTarget, w.LightLatencyP50, httpLight)
	}
	for _, b := range p.Targets {
		if b.Target != "generic_http" && b.LatencyP50.OK() {
			t.Errorf("%s shows a latency; only generic_http may", b.Target)
		}
	}
	// Snapshot 69 on, the window's latency is the latency target's alone, so the
	// series is charted.
	charted := false
	for _, pt := range w.Latency {
		charted = charted || pt.P50.OK()
	}
	if !charted {
		t.Errorf("the HTTP-only window latency series should be charted")
	}
	if got := model.StandingRank(p.Score.Rank, p.Score.Composite, p.Score.Peers); got != 1 {
		t.Errorf("a ready score alone in its kind stands %v", got)
	}
	if p.Pricing.PricePerGbAtScale != 0.75 || p.Pricing.PricePerGb != 1 {
		t.Errorf("pricing %+v", p.Pricing)
	}
	if !p.Pricing.CostPer1kSuccess.OK() || !w.CostPer1kSuccess.OK() {
		t.Errorf("a priced measured network needs a cost: %v / %v", p.Pricing.CostPer1kSuccess, w.CostPer1kSuccess)
	}
	if p.Trust.ResidentialAuthenticity != 0.987 || p.Trust.ClaimGap.OK() {
		t.Errorf("trust %+v", p.Trust)
	}
	for _, r := range build.Ranges {
		if valueRank(res, r.ID, "shifter") == 0 {
			t.Errorf("%s rankings leave the priced measured network without a value rank", r.ID)
		}
	}
}

// fixtureOutages: a 2-hour pulse outage in the US that ended 4h25m before
// as_of, one still open in DE for the last 30 minutes, and a 2-hour one ten
// days back (only in the 30d window). Two countries are tested.
var fixtureOutages = []map[string]any{
	{"country": "DE", "region": nil, "source": "census", "started_at": "2026-09-26T13:55:00+00:00", "ended_at": nil, "duration_s": 1800.0},
	{"country": "US", "region": "nyc1", "source": "pulse", "started_at": "2026-09-26T08:00:00+00:00", "ended_at": "2026-09-26T10:00:00+00:00", "duration_s": 7200.0},
	{"country": "DE", "region": "fra1", "source": "pulse", "started_at": "2026-09-16T12:00:00+00:00", "ended_at": "2026-09-16T14:00:00+00:00", "duration_s": 7200.0},
}

// withOutages is the committed fixture snapshot as v2 with fixtureOutages and
// the matching time-based uptime per window.
func withOutages(t *testing.T) []byte {
	t.Helper()
	body, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot.json"))
	if err != nil {
		t.Fatal(err)
	}
	var s map[string]any
	if err := json.Unmarshal(body, &s); err != nil {
		t.Fatal(err)
	}
	s["schema_version"] = 2
	product := s["products"].([]any)[0].(map[string]any)
	product["outages"] = fixtureOutages
	downtime := map[string]float64{"24h": 9000, "7d": 9000, "30d": 16200}
	for label, w := range product["windows"].(map[string]any) {
		w := w.(map[string]any)
		hours := w["period"].(map[string]any)["hours"].(float64)
		value := 1 - downtime[label]/(hours*3600*2)
		w["metrics"].(map[string]any)["uptime"] = value
		w["uptime"] = map[string]any{"batches": 48, "up": 47, "value": value, "downtime_s": downtime[label], "basis": "outage time", "countries": 2}
		points := w["series"].([]any)
		for i, point := range points {
			point := point.(map[string]any)
			point["uptime"] = nil
			if i == len(points)-1 {
				point["uptime"] = 0.9995
			}
		}
	}
	out, err := json.Marshal(s)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// The committed v1 fixture published over the test catalog (with its
// leftover is_demo network): the insight totals count only the measured network.
func TestMeasuredTotalsCountOnlyTheMeasuredNetwork(t *testing.T) {
	t.Parallel()
	snapshot, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot.json"))
	if err != nil {
		t.Fatal(err)
	}
	publishedBuild(t, snapshot)
}

// publishedBuild publishes snapshot over the test catalog, builds it and
// checks that the measured-only insight totals cover exactly the one measured
// provider.
func publishedBuild(t *testing.T, snapshot []byte) *build.Result {
	t.Helper()
	db := catalogued(t)
	db.PublishV4(t, snapshot)
	res := buildFrom(t, db)
	var measured *model.Provider
	for _, p := range res.Dataset.Providers {
		if p.Source == "measured" {
			measured = p
		}
	}
	if measured == nil {
		t.Fatal("the published snapshot gives no measured provider")
	}
	if res.Dataset.MeasuredTotals.Providers != 1 {
		t.Errorf("insight totals include other providers: %+v", res.Dataset.MeasuredTotals)
	}
	if res.Dataset.MeasuredTotals.MisclassifiedShare != measured.Trust.DatacenterAsnShare {
		t.Errorf("insight misclassification %v, measured provider %v", res.Dataset.MeasuredTotals.MisclassifiedShare, measured.Trust.DatacenterAsnShare)
	}
	return res
}

type Num = jsnum.Num
