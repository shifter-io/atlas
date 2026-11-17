package build

import (
	"math"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// poolContext is a build of one network, alpha, whose headline product has
// month as its 30d window and day as its 24h one, with the stats a build
// gives them: the pool charts of ipPoints.
func poolContext(month, day snap.Window) *Context {
	stats := map[string]*WindowStats{}
	for id, w := range map[string]snap.Window{"30d": month, "24h": day} {
		end := snap.Ms(w.Period.End)
		age := func(at string) Num { return Num((end - snap.Ms(at)) / 60000) }
		stats[id] = &WindowStats{SuccessDeltaPts: nan, LatencyDeltaPct: nan, Pool: ipPoints(w, id, w.Series, age)}
	}
	sp := &snap.Product{ID: "alpha-res", Windows: map[string]snap.Window{"30d": month, "24h": day}}
	p := &model.Provider{Slug: "alpha", Name: "Alpha Net", Source: model.SourceMeasured, PrimaryKind: "residential"}
	return &Context{GeneratedAt: changeAsOf, Stats: map[string]map[string]*WindowStats{"alpha": stats}, Pages: &Pages{},
		Entries: []*entry{{P: p, Snap: map[string]*snap.Product{"residential": sp}}}, Inputs: &store.Inputs{}}
}

// poolEntryOf is the pool entry of c's range rangeID, false without one.
func poolEntryOf(c *Context, rangeID string) (ChangeEntry, bool) {
	for _, r := range Ranges {
		if r.ID != rangeID {
			continue
		}
		for _, en := range changeEntries(c, r) {
			if en.Kind == "pool" {
				return en, true
			}
		}
	}
	return ChangeEntry{}, false
}

// staging30d is Proxy-Cheap's 30d daily rows on staging (17 Nov, 03:05): no
// history before the database copy, then the day measurement started in, a
// day, and the day ending at the cutoff. Its pool chart reads 455 to 180,624.
func staging30d(extra ...[2]Num) snap.Window {
	rows := make([][2]Num, 27, 31)
	rows = append(rows, [2]Num{3598, 270}, [2]Num{206538, 76365}, [2]Num{636520, 107077})
	return dailyPool(append(rows, extra...)...)
}

// CHANGELOG-REAL-1 (#1314): a pool entry needs two complete sides. The
// staging case made pool_grew +39,597.6% from the day measurement started
// in; with it left out, one counted day remains (the newest ends at the
// cutoff) and nothing is logged. A later day sampled three times as hard
// is no comparison either; only days sampled alike are.
func TestAPoolEntryNeedsCompleteSides(t *testing.T) {
	t.Parallel()
	day := changeWindow(0, nan)
	if en, ok := poolEntryOf(poolContext(staging30d(), day), "30d"); ok {
		t.Errorf("a pool entry from a partial day: %+v", en.Values)
	}
	if en, ok := poolEntryOf(poolContext(staging30d([2]Num{650000, 110000}), day), "30d"); ok {
		t.Errorf("a pool entry from sides sampled 206,538 against 636,520 requests a day: %+v", en.Values)
	}
	alike := dailyPool([2]Num{0, 0}, [2]Num{3598, 270}, [2]Num{600000, 100000}, [2]Num{620000, 125000}, [2]Num{650000, 900000})
	en, ok := poolEntryOf(poolContext(alike, day), "30d")
	if !ok || en.Event != "pool_grew" || en.Values["deltaPct"] != Num(25) || en.Values["previousUniqueIps"] != Num(100000) || en.AgoSeconds != 86400 {
		t.Errorf("days sampled alike: %+v %v", en, ok)
	}
}

// Each rule of a counted day alone: the window's first row (its day before
// is unknown), a day after one without requests, a day without IPs and the
// day ending at the cutoff are left out; a window that never ran counts none.
func TestCountedDays(t *testing.T) {
	t.Parallel()
	w := dailyPool([2]Num{100, 50}, [2]Num{100, 60}, [2]Num{0, 0}, [2]Num{100, 70}, [2]Num{100, 80}, [2]Num{100, 90})
	w.Series[4].UniqueIps = nan
	got := countedDays(w)
	if len(got) != 1 || got[0].ips != 60 || got[0].end != snap.Ms("2026-11-06T12:00:00Z") {
		t.Errorf("counted %+v, want only the second day (60 IPs, ending 6 Nov 12:00)", got)
	}
	w.Metrics.Completed = 0
	if got := countedDays(w); len(got) != 0 {
		t.Errorf("a window that never ran counts %+v", got)
	}
}

// The 24h pool compares released hours (whole hours, no request counts),
// dated at the newest hour's end; without them, its series rows (five
// minutes, or the calculator's hours) with requests and IPs, which must be
// sampled alike.
func TestThe24hPoolComparesWholeHours(t *testing.T) {
	t.Parallel()
	day := changeWindow(50, nan)
	day.Estimated = &snap.Estimated{BreakdownScale: nan, Series: []snap.EstimatedHour{{Start: "2026-11-10T08:00:00Z", UniqueIps: 100},
		{Start: "2026-11-10T09:00:00Z", UniqueIps: nan}, {Start: "2026-11-10T10:00:00Z", UniqueIps: 150}}}
	en, ok := poolEntryOf(poolContext(changeWindow(0, nan), day), "24h")
	if !ok || en.Values["deltaPct"] != Num(50) || en.AgoSeconds != 3600 {
		t.Errorf("24h hours: %+v %v", en, ok)
	}
	day.Estimated = nil
	day.Series = []snap.SeriesRow{{At: "2026-11-10T10:00:00Z", Completed: 10, UniqueIps: 10}, {At: "2026-11-10T10:05:00Z", Completed: 0, UniqueIps: nan},
		{At: "2026-11-10T11:00:00Z", Completed: 12, UniqueIps: 15}}
	en, ok = poolEntryOf(poolContext(changeWindow(0, nan), day), "24h")
	if !ok || en.Values["deltaPct"] != Num(50) || en.AgoSeconds != 3300 {
		t.Errorf("24h rows: %+v %v", en, ok)
	}
	day.Series[2].Completed = 40
	if en, ok := poolEntryOf(poolContext(changeWindow(0, nan), day), "24h"); ok {
		t.Errorf("24h rows sampled 10 against 40 requests: %+v", en)
	}
	// The calculator's hourly rows end an hour after they start, not five
	// minutes (SERIES-HOURLY-DAILY-1).
	day.Series = []snap.SeriesRow{{At: "2026-11-10T09:00:00Z", Completed: 10, UniqueIps: 10}, {At: "2026-11-10T10:00:00Z", Completed: 12, UniqueIps: 15}}
	en, ok = poolEntryOf(poolContext(changeWindow(0, nan), day), "24h")
	if !ok || en.Values["deltaPct"] != Num(50) || en.AgoSeconds != 3600 {
		t.Errorf("24h hourly rows: %+v %v, want +50%% dated at 11:00", en, ok)
	}
}

// A comparison whose opening side has no IPs, or sides without request
// counts, follow the same rules as the others.
func TestSameSamplingAndAnEmptyOpening(t *testing.T) {
	t.Parallel()
	hours := []poolSlot{{ips: 1, requests: math.NaN()}}
	if !sameSampling(hours, hours) || !sameSampling([]poolSlot{{requests: 100}}, []poolSlot{{requests: 50}}) ||
		sameSampling([]poolSlot{{requests: 100}}, []poolSlot{{requests: 49}}) {
		t.Error("sameSampling: hours are alike, 100 and 50 requests are, 100 and 49 are not")
	}
	w := changeWindow(50, nan)
	w.Estimated = &snap.Estimated{BreakdownScale: nan, Series: []snap.EstimatedHour{{Start: "2026-11-10T08:00:00Z", UniqueIps: 0},
		{Start: "2026-11-10T10:00:00Z", UniqueIps: 150}}}
	if d, _, at := poolChange(w, "24h"); !math.IsNaN(d) || !math.IsNaN(at) {
		t.Errorf("an opening hour without IPs moved %v at %v", d, at)
	}
}
