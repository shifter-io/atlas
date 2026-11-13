package build

import (
	"encoding/json"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// figuresWindow is a window ending at 2026-10-01T12:00 whose success history
// starts at each of starts.
func figuresWindow(start string, starts ...string) snap.Window {
	w := snap.EmptyWindow()
	w.Period = snap.Period{Start: start, End: "2026-10-01T12:00:00Z"}
	for _, at := range starts {
		w.Series = append(w.Series, snap.SeriesRow{At: at, SuccessRate: 0.9})
	}
	return w
}

func TestOverlapSharesFollowTheProviderRule(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		name              string
		exclusive, shared Num
		compared, ips     Num
		wantEx, wantSh    Num
	}{
		{"published shares pass through", 0.8, 0.2, 3, 100, 0.8, 0.2},
		{"an observed pool with no peer is wholly exclusive", nan, nan, 0, 100, 1, 0},
		{"no observed pool stays null", nan, nan, 0, nan, nan, nan},
		{"an empty pool stays null", nan, nan, 0, 0, nan, nan},
	} {
		w := snap.EmptyWindow()
		w.Overlap.ExclusiveShare, w.Overlap.SharedShare, w.Overlap.ComparedProducts = c.exclusive, c.shared, c.compared
		w.Pool.UniqueIps = c.ips
		ex, sh := overlapShares(w)
		if !sameNum(ex, c.wantEx) || !sameNum(sh, c.wantSh) {
			t.Errorf("%s: %v / %v, want %v / %v", c.name, ex, sh, c.wantEx, c.wantSh)
		}
	}
}

func sameNum(a, b Num) bool { return a == b || !a.OK() && !b.OK() }

func TestObservedDaysOfAWindowShorterThanItsRange(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		name   string
		window snap.Window
		want   Num
	}{
		{"under a day", figuresWindow("2026-09-24T12:00:00Z", "2026-10-01T00:00:00Z"), 0},
		{"3.5 days, rounded up", figuresWindow("2026-09-24T12:00:00Z", "2026-09-28T00:00:00Z", "2026-09-30T00:00:00Z"), 4},
		{"fully observed", figuresWindow("2026-09-24T12:00:00Z", "2026-09-24T12:00:00Z"), nan},
		{"no history", figuresWindow("2026-09-24T12:00:00Z"), nan},
	} {
		sp := &snap.Product{Windows: map[string]snap.Window{"7d": c.window}}
		if got := observedDays(sp, "7d"); !sameNum(got, c.want) {
			t.Errorf("%s: observedDays %v, want %v", c.name, got, c.want)
		}
	}
}

func TestLatencyUnderLoadFlagAndNote(t *testing.T) {
	t.Parallel()
	http := "generic_http"
	everyRequest := snap.Metrics{TimeSlots: &snap.TimeSlots{}}
	loaded := snap.Metrics{TimeSlots: &snap.TimeSlots{}, LatencyUnderLoadMs: &snap.Latency{P50: 1400}}
	for _, c := range []struct {
		name        string
		m           snap.Metrics
		head, light Num
		target      *string
		want        *bool
	}{
		{"under load", loaded, 1400, 900, &http, boolPtr(true)},
		{"light load only (older snapshot)", snap.Metrics{}, 900, nan, &http, boolPtr(false)},
		// No load test (production's cadence): the headline is the daily
		// tests' own latency, with no second latency beside it.
		{"light load only (no load test)", everyRequest, 900, nan, &http, boolPtr(false)},
		{"no latency", everyRequest, nan, nan, nil, nil},
	} {
		ws := &WindowStats{LatencyP50: c.head, LightLatencyP50: c.light, LatencyTarget: c.target}
		if got := latencyUnderLoad(c.m, ws); !sameBool(got, c.want) {
			t.Errorf("%s: latencyUnderLoad %v, want %v", c.name, got, c.want)
		}
	}
}

func boolPtr(b bool) *bool { return &b }

func sameBool(a, b *bool) bool { return a == nil && b == nil || a != nil && b != nil && *a == *b }

func TestUnderLoadSeriesPerBucket(t *testing.T) {
	t.Parallel()
	w := figuresWindow("2026-09-30T12:00:00Z", "2026-10-01T10:00:00Z", "2026-10-01T11:00:00Z")
	w.Metrics.TimeSlots = &snap.TimeSlots{LatencyTargets: []string{latencyTarget}}
	w.Series[0].LatencyUnderLoadMs = &snap.Latency{P50: 1400, P90: 1700, P99: 3900}
	age := func(at string) Num { return Num((snap.Ms(w.Period.End) - snap.Ms(at)) / 60000) }
	got := underLoadSeries(w, age)
	if len(got) != 2 || got[0] != (model.LatencyPoint{T: 120, P50: 1400, P90: 1700, P99: 3900}) || got[1].T != 60 || got[1].P50.OK() {
		t.Errorf("under-load series %+v", got)
	}
	w.Metrics.TimeSlots.LatencyTargets = []string{latencyTarget, "generic_https"}
	for _, p := range underLoadSeries(w, age) {
		if p.P50.OK() {
			t.Errorf("a mixed-target window charts latency under load: %+v", p)
		}
	}
}

func TestAPublishedProviderHasNoWindowFigures(t *testing.T) {
	t.Parallel()
	for _, r := range Ranges {
		ws := windowStats(&model.Provider{}, nil, r.ID, nil)
		raw, err := json.Marshal(ws.WindowFigures)
		if err != nil {
			t.Fatal(err)
		}
		var fields map[string]any
		if err := json.Unmarshal(raw, &fields); err != nil {
			t.Fatal(err)
		}
		for k, v := range fields {
			if v != nil {
				t.Errorf("%s: %s is %v, want null", r.ID, k, v)
			}
		}
		if len(fields) != 21 || ws.UnderLoad == nil || len(ws.UnderLoad) != 0 {
			t.Errorf("%s: %d figures, under-load series %v", r.ID, len(fields), ws.UnderLoad)
		}
	}
}

// Every chart answer labels each point of each history with the old site's
// bucketLabel, and a network's latency under load has a point per bucket.
func TestChartAnswersLabelEveryHistoryPoint(t *testing.T) {
	t.Parallel()
	pg := shapePages(t, testscale.Shape{Networks: 2, Countries: 1, Exits: 3, Tail: 5}, nil)
	if len(pg.Charts) == 0 {
		t.Fatal("no chart answers")
	}
	for key, c := range pg.Charts {
		lat := latencyLabels(c.Latency)
		if !sameLabels(c.Labels.Success, chartLabels(c.Success)) || !sameLabels(c.Labels.Availability, seriesLabels(c.Availability)) ||
			!sameLabels(c.Labels.Latency, lat) || !sameLabels(c.Labels.Pool, seriesLabels(c.Pool)) ||
			!sameLabels(c.Labels.LatencyUnderLoad, latencyLabels(c.LatencyUnderLoad)) || c.LatencyUnderLoad == nil {
			t.Errorf("%s: labels do not match its histories", key)
		}
		if c.Network != AllNetworks && len(c.LatencyUnderLoad) != len(c.Latency) {
			t.Errorf("%s: %d under-load points for %d latency points", key, len(c.LatencyUnderLoad), len(c.Latency))
		}
	}
}

func sameLabels(a, b []string) bool {
	if a == nil || len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// Without a load test in the window (production runs none), the headline
// latency is the daily tests' own light-load percentiles, P90 and P99
// included, and no light-load latency is shown beside it (API-NA-2).
func TestHeadlineLatencyWithoutALoadTestIsTheDailyTestsOwn(t *testing.T) {
	t.Parallel()
	light := snap.Latency{P50: 300, P90: 700, P99: 1500}
	under := snap.Latency{P50: 900, P90: 1800, P99: 4000}
	for _, c := range []struct {
		name         string
		m            snap.Metrics
		head, beside snap.Latency
	}{
		{"older snapshot", snap.Metrics{LatencyMs: light}, light, naLatency},
		{"every request, no load test", snap.Metrics{TimeSlots: &snap.TimeSlots{}, LatencyMs: light}, light, naLatency},
		{"every request, a load test without P50", snap.Metrics{TimeSlots: &snap.TimeSlots{}, LatencyMs: light,
			LatencyUnderLoadMs: &snap.Latency{P50: nan, P90: 1}}, light, naLatency},
		{"every request, under load", snap.Metrics{TimeSlots: &snap.TimeSlots{}, LatencyMs: light, LatencyUnderLoadMs: &under}, under, light},
	} {
		head, beside := headlineLatency(c.m), lightLatency(c.m)
		if head.P50 != c.head.P50 || head.P90 != c.head.P90 || head.P99 != c.head.P99 || !sameNum(beside.P50, c.beside.P50) ||
			!sameNum(beside.P99, c.beside.P99) {
			t.Errorf("%s: headline %+v beside %+v, want %+v and %+v", c.name, head, beside, c.head, c.beside)
		}
	}
}
