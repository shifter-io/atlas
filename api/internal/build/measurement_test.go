package build

import (
	"encoding/json"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// STANDING-FREEZE-1: a network window's standing is served as measurement,
// and a held window's history keeps its stored point scores.

func standingProduct(standing *snap.WindowStanding, rates []float64, stored []*snap.PointScore) *snap.Product {
	times := []string{"2026-11-19T10:00:00+00:00", "2026-11-19T10:05:00+00:00"}
	rows := make([]snap.SeriesRow, len(rates))
	for i, rate := range rates {
		rows[i] = snap.SeriesRow{At: times[i], Completed: 200, Successes: Num(200 * rate), SuccessRate: Num(rate)}
		if stored != nil {
			rows[i].SuccessScore = stored[i]
		}
	}
	return &snap.Product{Windows: map[string]snap.Window{"24h": {Series: rows, Standing: standing}}}
}

func TestMeasurementServesTheWindowStanding(t *testing.T) {
	last, from := "2026-11-19T13:10:00+00:00", "2026-11-19T13:00:00+00:00"
	frozen := &snap.WindowStanding{Status: snap.StandingFrozen, LastMeasured: &last, FrozenSince: &last, ValuesFrom: &from}
	got, _ := json.Marshal(measurement(standingProduct(frozen, nil, nil), "24h"))
	want := `{"status":"frozen","lastMeasuredAt":"2026-11-19T13:10:00.000Z","frozenSince":"2026-11-19T13:10:00.000Z",` +
		`"valuesAsOf":"2026-11-19T13:00:00.000Z"}`
	if string(got) != want {
		t.Fatalf("frozen: %s, want %s", got, want)
	}
	none := `{"status":null,"lastMeasuredAt":null,"frozenSince":null,"valuesAsOf":null}`
	for name, sp := range map[string]*snap.Product{"no product": nil, "older snapshot": standingProduct(nil, nil, nil)} {
		if got, _ := json.Marshal(measurement(sp, "24h")); string(got) != none {
			t.Errorf("%s: %s, want %s", name, got, none)
		}
	}
	measured := &snap.WindowStanding{Status: snap.StandingMeasured, LastMeasured: &last}
	if standingProduct(measured, nil, nil).Window("24h").Held() || !standingProduct(frozen, nil, nil).Window("24h").Held() {
		t.Error("only a frozen or stopped window is held")
	}
}

func TestAHeldWindowKeepsItsStoredPointScoresAndIsNoPeer(t *testing.T) {
	src := func(slug string, sp *snap.Product) rankingSource {
		stats := map[string]*WindowStats{"24h": {Success: make([]model.SeriesPoint, 2)}}
		return rankingSource{p: &model.Provider{Slug: slug}, kind: "residential", sp: sp, stats: stats}
	}
	measured := &snap.WindowStanding{Status: snap.StandingMeasured}
	frozen := &snap.WindowStanding{Status: snap.StandingFrozen}
	ten := &snap.PointScore{Score: 10, Status: scoreRanked}
	srcs := []rankingSource{
		src("a", standingProduct(measured, []float64{0.9, 0.9}, nil)),
		src("b", standingProduct(measured, []float64{0.6, 0.6}, nil)),
		src("c", standingProduct(nil, []float64{0.3, 0.3}, nil)),
		src("d", standingProduct(frozen, []float64{0.1, 0.1}, []*snap.PointScore{ten, nil})),
	}
	scores := rankingPointScores(srcs, "24h")
	for slug, want := range map[string]float64{"a": 10, "b": 5.5, "c": 1} {
		for i, s := range scores[slug] {
			if s.Score.F() != want || s.Status != scoreRanked || s.RankedOf.F() != 3 {
				t.Errorf("%s point %d: %+v, want %v of 3 (the frozen network is no peer)", slug, i, s, want)
			}
		}
	}
	held := scores["d"]
	if held[0].Score.F() != 10 || held[0].Status != scoreRanked || held[1].Score.OK() || held[1].Status != notEnoughData {
		t.Errorf("frozen: %+v, want its stored 10, then not_enough_data", held)
	}
}
