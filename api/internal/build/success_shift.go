package build

import (
	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
)

// The success score shift (SUCCESS-SHIFT-1): in each group of success
// scores, every network's score gains the same offset, 10 − the group's best,
// so the best reads 10 and the others keep their measured distance below it.
// Order, ranks, statuses and nulls are unchanged; nothing goes above 10 or
// below 1. The groups are an overall's kind and range (the browser overall's
// also its engine), a day's kind and day, and a target score's kind, range,
// target, engine and metric. The success overall's scores are already
// shifted by the calculation (SUCCESS-ALL-8-1: a window's, its days' and its
// chart points' alike), so their best reads 10 and the shift leaves them as
// they are. The calculation's payload keeps the measured
// scores: the build shifts its copy once (shiftSuccessScores), and the scores
// it ranks itself are shifted as they are made (shiftedRankScore). A
// previous build's score moves with its group's offset, so a delta stays the
// network's measured change. The absolute scores (absoluteScore) are not
// shifted. A held window (snap.Window.Held(): a network not measured,
// STANDING-FREEZE-1) is in no group: its scores are served as stored, so a
// frozen network's never move with another network's best, and it moves no
// group's best (as the calculation keeps it out of every peer set); a
// network whose 30d window is held keeps its days as stored too.

// bestShownScore is what the best score of a group reads.
const bestShownScore = 10

// shiftedRankScore is rankScore shifted so the best rank among m reads 10.
func shiftedRankScore(rank, best, m float64) Num {
	return rankScore(rank, m) + (bestShownScore - rankScore(best, m))
}

// scoreShift is one pass over the snapshot's success scores: the first finds
// each group's best, the second (apply) adds each group's offset.
type scoreShift struct {
	best  map[string]Num
	apply bool
}

// shiftSuccessScores shifts the snapshot's success scores in place.
func shiftSuccessScores(s *snap.Snapshot) {
	if s == nil {
		return
	}
	sh := &scoreShift{best: map[string]Num{}}
	for _, sh.apply = range []bool{false, true} {
		for i := range s.Products {
			sh.product(&s.Products[i])
		}
	}
}

// member counts a group member's score and shifts it and its previous score.
func (sh *scoreShift) member(group string, score, previous *Num) {
	if !sh.apply {
		if b, ok := sh.best[group]; score.OK() && (!ok || *score > b) {
			sh.best[group] = *score
		}
		return
	}
	b, ok := sh.best[group]
	if !ok {
		return
	}
	sh.follow(group, previous)
	if *score == b {
		// The best reads exactly 10, never 9.999… from the addition.
		*score = bestShownScore
		return
	}
	sh.follow(group, score)
}

// follow shifts a score by its group's offset without counting it.
func (sh *scoreShift) follow(group string, v *Num) {
	if b, ok := sh.best[group]; sh.apply && ok && v != nil && v.OK() {
		*v += bestShownScore - b
	}
}

func (sh *scoreShift) product(p *snap.Product) {
	for label, w := range p.Windows {
		if w.Held() { // its stored scores, and no member of a group's best
			continue
		}
		sh.window(p.Kind+"|"+label, w)
	}
	if p.Window("30d").Held() { // its days are the frozen 30d window's
		return
	}
	for _, d := range p.Days {
		if o := d.SuccessOverall; o != nil {
			sh.member("day|"+p.Kind+"|"+dayOf(d.Start), &o.Score, &o.PreviousScore)
		}
	}
}

func (sh *scoreShift) window(scope string, w snap.Window) {
	for id, t := range w.Targets {
		target := scope + "|" + id + "|"
		sh.targetScores(target+model.EngineHTTP, t.Scores)
		if br := t.Browser; br != nil {
			sh.targetScores(target+br.Engine, br.Scores)
			for engine, e := range br.Engines {
				sh.targetScores(target+engine, e.Scores)
			}
		}
		// The challenge overall's own target scores: an HTTP target's, a
		// browser target's main engine's. The success overall's are its card
		// targets' distance scores (SUCCESS-ALL-8-1), whose best already
		// reads 10: no target score's offset moves them.
		engine := model.EngineHTTP
		if t.Browser != nil {
			engine = t.Browser.Engine
		}
		sh.overallTarget(w.ChallengeOverall, id, target+engine+"|challenge")
	}
	for engine, o := range w.BrowserOverall {
		sh.member("browser|"+scope+"|"+engine, &o.Success.Score, nil)
		w.BrowserOverall[engine] = o
	}
	if o := w.SuccessOverall; o != nil {
		sh.member("overall|"+scope, &o.Score, &o.PreviousScore)
	}
	if o := w.ChallengeOverall; o != nil {
		sh.member("challenge_overall|"+scope, &o.Score, &o.PreviousScore)
	}
}

// overallTarget shifts target id's score in the overall o (nil: none) as
// its target score's group.
func (sh *scoreShift) overallTarget(o *snap.SuccessOverall, id, group string) {
	if o == nil {
		return
	}
	if v, ok := o.Targets[id]; ok {
		sh.follow(group, &v)
		o.Targets[id] = v
	}
}

// targetScores shifts one engine's scores of a target, each metric its own
// group.
func (sh *scoreShift) targetScores(group string, s *snap.TargetScores) {
	if s == nil {
		return
	}
	sh.member(group+"|success", &s.Success.Score, &s.Success.PreviousScore)
	sh.member(group+"|challenge", &s.Challenge.Score, &s.Challenge.PreviousScore)
	sh.member(group+"|block", &s.Block.Score, &s.Block.PreviousScore)
}
