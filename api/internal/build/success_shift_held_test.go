package build

import (
	"encoding/json"
	"testing"

	"github.com/shifter-io/atlas/api/internal/snap"
)

// STANDING-FREEZE-1: a frozen network's window is in no shift group. Its
// browser overall, target scores, challenge overall and days are served as
// stored, whatever the other networks' best; it moves no group's best.

// heldProduct is shiftProduct with a challenge overall and, when frozen, a
// frozen 30d window.
func heldProduct(target Num, frozen bool) snap.Product {
	p := shiftProduct(10, target, target)
	w := p.Windows["30d"]
	w.ChallengeOverall = &snap.SuccessOverall{Score: target, Rank: nan, RankedOf: nan, Status: scoreRanked, PreviousScore: nan,
		Targets: map[string]Num{"google_search": target}, Weights: map[string]Num{"google_search": 4}}
	if frozen {
		w.Standing = &snap.WindowStanding{Status: snap.StandingFrozen}
	} else {
		w.Standing = &snap.WindowStanding{Status: snap.StandingMeasured}
	}
	p.Windows["30d"] = w
	return p
}

// served is a product's window and days as the answers read them, as JSON.
func served(t *testing.T, p snap.Product) string {
	t.Helper()
	b, err := json.Marshal(struct {
		W    snap.Window
		Days []snap.Day
	}{p.Window("30d"), p.Days})
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestAFrozenNetworksScoresAreServedAsStored(t *testing.T) {
	frozen := heldProduct(9.5, true) // above the measured best: no member of it
	stored := served(t, heldProduct(9.5, true))
	for _, best := range []Num{8, 7} { // another network's best moves
		s := &snap.Snapshot{Products: []snap.Product{heldProduct(best, false), heldProduct(6, false), frozen}}
		shiftSuccessScores(s)
		if got := served(t, s.Products[2]); got != stored {
			t.Errorf("best %v: the frozen window moved:\n%s\nwant\n%s", best, got, stored)
		}
		w := s.Products[1].Window("30d")
		want := 6 + 10 - best.F() // the measured best reads 10; the frozen 9.5 is no member
		if !near(w.BrowserOverall["camoufox"].Success.Score, want) || !near(w.ChallengeOverall.Score, want) ||
			!near(w.Targets["google_search"].Browser.Scores.Success.Score, want) {
			t.Errorf("best %v: measured scores %+v, want %v", best, w.BrowserOverall, want)
		}
		frozen = s.Products[2]
	}
}

func TestMeasuredNetworksAreShiftedAsWithoutTheFrozenOne(t *testing.T) {
	with := &snap.Snapshot{Products: []snap.Product{heldProduct(8, false), heldProduct(6, false), heldProduct(9.5, true)}}
	without := &snap.Snapshot{Products: []snap.Product{heldProduct(8, false), heldProduct(6, false)}}
	shiftSuccessScores(with)
	shiftSuccessScores(without)
	for i := range without.Products {
		if a, b := served(t, with.Products[i]), served(t, without.Products[i]); a != b {
			t.Errorf("network %d: %s\nwant %s", i, a, b)
		}
	}
	// A measured network's standing changes nothing: the same shift as before it.
	plain := &snap.Snapshot{Products: []snap.Product{shiftProduct(10, 8, 8), shiftProduct(10, 6, 6)}}
	shiftSuccessScores(plain)
	for i := range plain.Products {
		w, v := with.Products[i].Window("30d"), plain.Products[i].Window("30d")
		if w.BrowserOverall["camoufox"].Success.Score != v.BrowserOverall["camoufox"].Success.Score {
			t.Errorf("network %d: %v, want %v", i, w.BrowserOverall, v.BrowserOverall)
		}
	}
}
