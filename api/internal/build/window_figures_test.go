package build

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
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

// latencyUnderLoad is false wherever there is a latency, as the headline is
// the pulses' (HEADLINE-PULSE-1), and null without one.
func TestLatencyUnderLoadFlagAndNote(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		name string
		head Num
		want *bool
	}{
		{"a latency", 900, boolPtr(false)},
		{"no latency", nan, nil},
	} {
		if got := latencyUnderLoad(&WindowStats{LatencyP50: c.head}); !sameBool(got, c.want) {
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
		if len(fields) != 24 || ws.UnderLoad == nil || len(ws.UnderLoad) != 0 {
			t.Errorf("%s: %d figures, under-load series %v", r.ID, len(fields), ws.UnderLoad)
		}
	}
}

// Every chart answer gives each point of each history its time (t), with no
// English point labels (UNREAD-TEXT-1, #977), and a network's latency under
// load has a point per bucket.
func TestChartAnswersTimeEveryHistoryPoint(t *testing.T) {
	t.Parallel()
	pg := shapePages(t, testscale.Shape{Networks: 2, Countries: 1, Exits: 3, Tail: 5}, nil)
	if len(pg.Charts) == 0 {
		t.Fatal("no chart answers")
	}
	for key, c := range pg.Charts {
		times := []Num{}
		for _, p := range c.Success {
			times = append(times, p.T)
		}
		for _, l := range [][]model.LatencyPoint{c.Latency, c.LatencyUnderLoad} {
			for _, p := range l {
				times = append(times, p.T)
			}
		}
		for _, l := range [][]model.SeriesPoint{c.Availability, c.Pool} {
			for _, p := range l {
				times = append(times, p.T)
			}
		}
		for _, v := range times {
			if !v.OK() || v < 0 {
				t.Errorf("%s: a point's time is %v", key, v)
			}
		}
		if b, err := json.Marshal(c); err != nil || strings.Contains(string(b), `"labels"`) || c.LatencyUnderLoad == nil {
			t.Errorf("%s: %v, labels served or no under-load series: %.200s", key, err, b)
		}
		if c.Network != AllNetworks && len(c.LatencyUnderLoad) != len(c.Latency) {
			t.Errorf("%s: %d under-load points for %d latency points", key, len(c.LatencyUnderLoad), len(c.Latency))
		}
	}
}

// The headline latency is the normal requests' (the pulses') percentiles,
// P90 and P99 included, with or without a load test in the window; the
// latency under load is served beside it, display only (HEADLINE-PULSE-1).
func TestHeadlineLatencyIsThePulses(t *testing.T) {
	t.Parallel()
	light := snap.Latency{P50: 300, P90: 700, P99: 1500}
	under := snap.Latency{P50: 900, P90: 1800, P99: 4000}
	for _, c := range []struct {
		name   string
		m      snap.Metrics
		beside snap.Latency
	}{
		{"older snapshot", snap.Metrics{LatencyMs: light, LatencyUnderLoadMs: &under}, naLatency},
		{"every request, no load test", snap.Metrics{TimeSlots: &snap.TimeSlots{}, LatencyMs: light}, naLatency},
		{"every request, a load test without P50", snap.Metrics{TimeSlots: &snap.TimeSlots{}, LatencyMs: light,
			LatencyUnderLoadMs: &snap.Latency{P50: nan, P90: 1}}, naLatency},
		{"every request, under load", snap.Metrics{TimeSlots: &snap.TimeSlots{}, LatencyMs: light, LatencyUnderLoadMs: &under}, under},
	} {
		head, beside := headlineLatency(c.m), underLoadLatency(c.m)
		if head.P50 != light.P50 || head.P90 != light.P90 || head.P99 != light.P99 || !sameNum(beside.P50, c.beside.P50) ||
			!sameNum(beside.P90, c.beside.P90) || !sameNum(beside.P99, c.beside.P99) {
			t.Errorf("%s: headline %+v under load %+v, want %+v and %+v", c.name, head, beside, light, c.beside)
		}
	}
}

// An outage band is its span, severity code and country code, never an
// English label (UNREAD-TEXT-1, #977); an incident without a country has a
// null one.
func TestOutageBandsAreCodes(t *testing.T) {
	t.Parallel()
	av := availabilityAt([]model.Incident{
		{StartedAgoSeconds: 7200, DurationMinutes: 30, Severity: model.S("major"), Scope: model.S("AD"), Impact: 1},
		{StartedAgoSeconds: 3600, DurationMinutes: 10, Severity: model.S("minor"), Impact: 1}}, 1)
	b, err := json.Marshal(av.bands)
	if err != nil || len(av.bands) != 2 {
		t.Fatalf("bands %s %v", b, err)
	}
	want := `[{"from":0.9166666666666666,"to":0.9375,"severity":"major","country":"AD"},` +
		`{"from":0.9583333333333334,"to":0.9652777777777778,"severity":"minor","country":null}]`
	if string(b) != want {
		t.Errorf("bands %s, want %s", b, want)
	}
}
