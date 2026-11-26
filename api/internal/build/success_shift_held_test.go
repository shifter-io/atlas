package build

import (
	"encoding/json"
	"testing"

	"github.com/shifter-io/atlas/api/internal/snap"
)

// STANDING-FREEZE-2: a frozen network's window is in no shift group: it
// moves no group's best, and its browser overall, target scores and challenge
// overall are shifted by its own stored bests (those of the build its values
// are from), so they serve the last published values; without bests (a
// stopped window) they are served as stored.

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

// bestsOf is the calculation's standing.bests of heldProduct windows whose
// scores' best is best (calculated/standing.py bests()).
func bestsOf(best Num) map[string]Num {
	out := map[string]Num{"browser|camoufox": best, "challenge_overall": best}
	for _, metric := range []string{"success", "challenge", "block"} {
		for _, group := range []string{"google_search|camoufox", "generic_http|http"} {
			out["target|"+group+"|"+metric] = best
		}
	}
	return out
}

// servedScores is what a window's shifted scores serve, standing aside.
func servedScores(t *testing.T, p snap.Product) string {
	t.Helper()
	w := p.Window("30d")
	b, err := json.Marshal([]any{w.BrowserOverall, w.Targets, w.ChallengeOverall, w.SuccessOverall})
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// The served value at the first frozen build equals the last measured
// build's: the frozen window is shifted by the bests of the build its values
// are from, not by the current group's, and holds while the best moves.
func TestAFrozenNetworkServesItsLastPublishedScores(t *testing.T) {
	measured := &snap.Snapshot{Products: []snap.Product{heldProduct(8, false), heldProduct(6, false), heldProduct(7, false)}}
	shiftSuccessScores(measured)
	published := servedScores(t, measured.Products[2])
	if got := measured.Products[2].Window("30d").BrowserOverall["camoufox"].Success.Score; !near(got, 9) {
		t.Fatalf("published %v, want 7 + 10 - 8", got)
	}
	for _, best := range []Num{7.5, 9.5} { // the measured best falls, then rises past it
		frozen := heldProduct(7, true)
		frozen.Windows["30d"].Standing.Bests = bestsOf(8)
		s := &snap.Snapshot{Products: []snap.Product{heldProduct(best, false), heldProduct(6, false), frozen}}
		shiftSuccessScores(s)
		if got := servedScores(t, s.Products[2]); got != published {
			t.Errorf("best %v: frozen serves\n%s\nwant the last published\n%s", best, got, published)
		}
		if got := s.Products[0].Window("30d").BrowserOverall["camoufox"].Success.Score; got != 10 {
			t.Errorf("best %v: the measured best reads %v, want 10 (the frozen one is no member)", best, got)
		}
	}
}

// The calculation's bests decode as the API reads them.
func TestStandingBestsDecode(t *testing.T) {
	var w snap.WindowStanding
	if err := json.Unmarshal([]byte(`{"status":"frozen","bests":{"browser|camoufox":8.25,"target|generic_http|http|success":null}}`), &w); err != nil {
		t.Fatal(err)
	}
	if w.Bests["browser|camoufox"] != 8.25 || w.Bests["target|generic_http|http|success"].OK() {
		t.Errorf("bests %v", w.Bests)
	}
}

// With the ranks the calculation gives (standing.py rerank(): on the served
// values), a descending rank never serves a rising browser or challenge
// overall while a frozen network is ranked among the measured ones.
func TestRanksAndServedOverallsAgreeWithAFrozenNetwork(t *testing.T) {
	ranked := func(p snap.Product, rank Num) snap.Product {
		w := p.Windows["30d"]
		o := w.BrowserOverall["camoufox"]
		o.Success.Rank = rank
		w.BrowserOverall["camoufox"] = o
		w.ChallengeOverall.Rank = rank
		p.Windows["30d"] = w
		return p
	}
	frozen := heldProduct(4.5, true) // published as 9.5: its group's best was 5
	frozen.Windows["30d"].Standing.Bests = bestsOf(5)
	s := &snap.Snapshot{Products: []snap.Product{
		ranked(heldProduct(9.8, false), 1), ranked(frozen, 2), ranked(heldProduct(4.6, false), 3),
	}}
	shiftSuccessScores(s)
	reads := map[string]func(snap.Window) Num{
		"browserOverall":   func(w snap.Window) Num { return w.BrowserOverall["camoufox"].Success.Score },
		"challengeOverall": func(w snap.Window) Num { return w.ChallengeOverall.Score },
	}
	for name, read := range reads {
		last := Num(11)
		for i, p := range s.Products { // in rank order
			if v := read(p.Window("30d")); v > last {
				t.Errorf("%s: rank %d serves %v, above rank %d's %v", name, i+1, v, i, last)
			} else {
				last = v
			}
		}
	}
	if v := s.Products[1].Window("30d").BrowserOverall["camoufox"].Success.Score; !near(v, 9.5) {
		t.Errorf("frozen serves %v, want its published 9.5", v)
	}
}
