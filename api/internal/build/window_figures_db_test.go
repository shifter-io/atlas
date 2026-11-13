package build_test

import (
	"os"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/build"
)

// Every per-range figure of the every-request fixture's network, read from
// its snapshot windows (each range publishes the same figures).
func TestWindowFiguresOfTheEveryRequestFixture(t *testing.T) {
	t.Parallel()
	snapshot, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil {
		t.Fatal(err)
	}
	res := publishedBuild(t, snapshot)
	for _, r := range build.Ranges {
		w := res.Details["shifter"].Windows["residential"][r.ID]
		f := w.WindowFigures
		got := []Num{f.CaptchaRate, f.BlockRate, f.RequestSuccessRate, f.Countries, f.Regions, f.Cities, f.Asns, f.DatacenterAsnShare, f.ExclusiveShare, f.SharedShare}
		want := []Num{0.004, 0.002, 0.9994, 3, 53, 1831, 335, 0.004, 1, 0}
		for i := range want {
			if got[i] != want[i] {
				t.Errorf("%s: figure %d is %v, want %v (figures %+v)", r.ID, i, got[i], want[i], f)
			}
		}
		// The history starts 23h05m before the end: under a day observed.
		if f.ObservedDays != 0 {
			t.Errorf("%s: observedDays %v, want 0", r.ID, f.ObservedDays)
		}
		if f.LatencyUnderLoad == nil || !*f.LatencyUnderLoad {
			t.Errorf("%s: latency under load %v", r.ID, f.LatencyUnderLoad)
		}
		if len(w.UnderLoad) != len(w.Latency) {
			t.Errorf("%s: %d under-load points for %d buckets", r.ID, len(w.UnderLoad), len(w.Latency))
		}
	}
}

// The v1 fixture's latency is light load (latency_ms, no every-request
// scoring), and it publishes no outcome rates, request success rate or wire
// bytes.
func TestWindowFiguresOfALightLoadSnapshot(t *testing.T) {
	t.Parallel()
	snapshot, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot.json"))
	if err != nil {
		t.Fatal(err)
	}
	res := publishedBuild(t, snapshot)
	for _, r := range build.Ranges {
		f := res.Details["shifter"].Windows["residential"][r.ID].WindowFigures
		if f.LatencyUnderLoad == nil || *f.LatencyUnderLoad {
			t.Errorf("%s: latency under load %v; want false", r.ID, f.LatencyUnderLoad)
		}
		if f.CaptchaRate.OK() || f.RequestSuccessRate.OK() || f.Cities != 1831 {
			t.Errorf("%s: figures %+v", r.ID, f)
		}
	}
}
