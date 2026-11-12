package build

import (
	"sort"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// Browser targets (browser pulse, K-BP1 and K-BP6). A browser target's
// benchmark adds its outcome shares and page-load times to the network
// target figures; a group target (ai_answers) adds its sources' breakdown.
// The figures are one engine's (engine); engines holds each engine's own.
// Scores rank them among the networks (TARGET-SCORES-1), next to the raw rates.
// Network-level figures never read them: the snapshot keeps browser runs out
// of every network metric, and the API maps only per-target fields here.

// naOutcomes is every outcome unavailable.
var naOutcomes = model.Outcomes{Ok: nan, Challenge: nan, Block: nan, LoginWall: nan, Timeout: nan, ProxyError: nan,
	NoContent: nan, NoAnswer: nan, OtherError: nan}

func outcomesOf(r snap.OutcomeRates) model.Outcomes {
	return model.Outcomes{Ok: r.Ok, Challenge: r.Challenge, Block: r.Block, LoginWall: r.LoginWall, Timeout: r.Timeout,
		ProxyError: r.ProxyError, NoContent: r.NoContent, NoAnswer: r.NoAnswer, OtherError: r.OtherError}
}

// targetGroups is the group each source target belongs to and each group
// target's sources: the snapshot methodology's target_groups, which name them
// before any browser run is measured, then what the product windows publish
// (targets.<id>.browser group and sources; the first value seen, products in
// order and windows by label, wins).
func targetGroups(s *snap.Snapshot) (group map[string]string, sources map[string][]string) {
	group, sources = map[string]string{}, map[string][]string{}
	if s == nil {
		return group, sources
	}
	for g, list := range s.Methodology.Settings.TargetGroups {
		sources[g] = list
		for _, id := range list {
			group[id] = g
		}
	}
	for i := range s.Products {
		p := &s.Products[i]
		labels := make([]string, 0, len(p.Windows))
		for l := range p.Windows {
			labels = append(labels, l)
		}
		sort.Strings(labels)
		for _, l := range labels {
			for id, t := range p.Windows[l].Targets {
				b := t.Browser
				if b == nil {
					continue
				}
				if _, ok := group[id]; !ok && b.Group != nil {
					group[id] = *b.Group
				}
				if _, ok := sources[id]; !ok && b.Sources != nil {
					sources[id] = b.Sources
				}
			}
		}
	}
	return group, sources
}

// frameworkEngine is a framework target's engine; "" (a source that does not
// say) is http, the column's default.
func frameworkEngine(t store.FrameworkTarget) model.Str {
	if t.Engine == "" {
		return model.S(model.EngineHTTP)
	}
	return model.S(t.Engine)
}

// addTargetGroups sets each target's group and sources from the snapshot.
func addTargetGroups(targets []model.TargetMeta, s *snap.Snapshot) {
	group, sources := targetGroups(s)
	for i := range targets {
		t := &targets[i]
		if g, ok := group[t.ID]; ok {
			t.Group = model.S(g)
		}
		t.Sources = sources[t.ID]
	}
}

// isBrowser reports whether a target's benchmark carries the browser fields:
// its framework engine is browser, or the window publishes it as one.
func isBrowser(t model.TargetMeta, tr snap.TargetResult, has bool) bool {
	return model.SV(t.Engine) == model.EngineBrowser || (has && tr.Browser != nil)
}

// browserBenchmark is a browser target's card figures in one window, with a
// group target's breakdown over its sources (each N/A where the window does
// not measure it), and its main engine's scores. labels names the targets.
func browserBenchmark(w snap.Window, t model.TargetMeta, labels map[string]model.Str) (*model.BrowserBenchmark, *model.TargetScores) {
	var scores *model.TargetScores
	b := &model.BrowserBenchmark{Outcomes: naOutcomes, PageLoadP50: nan, PageLoadP90: nan, Engines: map[string]model.EngineBenchmark{}}
	if tr, ok := w.Targets[t.ID]; ok && tr.Browser != nil {
		b.Outcomes = outcomesOf(tr.Browser.OutcomeRates)
		b.PageLoadP50, b.PageLoadP90 = tr.Browser.PageLoadMs.P50, tr.Browser.PageLoadMs.P90
		if tr.Browser.Engine != "" {
			b.Engine = model.S(tr.Browser.Engine)
		}
		scores = mainEngineScores(tr.Browser)
		for name, e := range tr.Browser.Engines {
			b.Engines[name] = model.EngineBenchmark{Requests: e.Completed, Successes: e.Successes,
				Outcomes: outcomesOf(e.OutcomeRates), PageLoadP50: e.PageLoadMs.P50, PageLoadP90: e.PageLoadMs.P90,
				Scores: targetScores(e.Scores)}
		}
		// The calculator keeps the previous score on the main engine's copy
		// only: that engine's own scores share its delta.
		if main := b.Engines[model.SV(b.Engine)]; scores != nil && main.Scores != nil && !main.Scores.Success.Delta.OK() {
			main.Scores.Success.Delta = scores.Success.Delta
		}
	}
	for _, id := range t.Sources {
		src := model.TargetSource{Target: id, Label: labels[id], SuccessRate: nan, Requests: 0, Outcomes: naOutcomes}
		if tr, ok := w.Targets[id]; ok {
			src.SuccessRate, src.Requests = tr.Metrics.SuccessRate, tr.Metrics.Completed
			if tr.Browser != nil {
				src.Outcomes, src.Scores = outcomesOf(tr.Browser.OutcomeRates), mainEngineScores(tr.Browser)
			}
		}
		b.Sources = append(b.Sources, src)
	}
	return b, scores
}

// mainEngineScores is a browser target's main engine's scores (the
// calculator's copy); a window that publishes only the engines' has them
// there. Nil (null) when the snapshot has none.
func mainEngineScores(br *snap.BrowserResult) *model.TargetScores {
	if s := targetScores(br.Scores); s != nil || br.Engine == "" {
		return s
	}
	return targetScores(br.Engines[br.Engine].Scores)
}

func metricScore(m snap.MetricScore) model.MetricScore {
	return model.MetricScore{Score: m.Score, Rank: m.Rank, RankedOf: m.RankedOf, Status: m.Status}
}

// targetSuccess is a target's success score with its delta from the previous
// published build's (SUCCESS-SCORE-DELTA-1).
func targetSuccess(m snap.MetricScore) model.TargetSuccess {
	return model.TargetSuccess{MetricScore: metricScore(m), Delta: scoreDelta(m.Score, m.PreviousScore)}
}

// targetScores is a browser target's scores in one engine; nil (null) when
// the snapshot has none.
func targetScores(s *snap.TargetScores) *model.TargetScores {
	if s == nil {
		return nil
	}
	challenge, block := metricScore(s.Challenge), metricScore(s.Block)
	return &model.TargetScores{Success: targetSuccess(s.Success), Challenge: &challenge, Block: &block}
}

// successScores is a network target's scores: success, and challenge and
// block when the snapshot has them (CHALLENGE-BLOCK-TIE-1; not written in an
// older one); nil (null) when the snapshot has none.
func successScores(s *snap.TargetScores) *model.TargetScores {
	if s == nil {
		return nil
	}
	out := &model.TargetScores{Success: targetSuccess(s.Success)}
	if s.Challenge.Status != "" {
		challenge, block := metricScore(s.Challenge), metricScore(s.Block)
		out.Challenge, out.Block = &challenge, &block
	}
	return out
}

// browserOverall is a window's overall browser score per engine; nil (not
// written) when the snapshot has none.
func browserOverall(w snap.Window) map[string]model.BrowserOverall {
	if w.BrowserOverall == nil {
		return nil
	}
	out := make(map[string]model.BrowserOverall, len(w.BrowserOverall))
	for engine, o := range w.BrowserOverall {
		s := o.Success
		success := model.OverallScore{MetricScore: model.MetricScore{Score: s.Score, Rank: s.Rank, RankedOf: s.RankedOf, Status: s.Status},
			Targets: o.Targets, Weights: o.Weights}
		if success.Targets == nil {
			success.Targets = []string{}
		}
		if success.Weights == nil {
			success.Weights = map[string]model.Num{}
		}
		out[engine] = model.BrowserOverall{Success: success}
	}
	return out
}
