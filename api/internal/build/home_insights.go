package build

import (
	"sort"

	"github.com/Zeus-Development/atlas/api/internal/model"
)

// The home's four insight cards (api/answers/home.md), ported from the old
// site's overview.ts overviewInsights over the range's ranking rows (W2), the
// target benchmarks and the overlap (W5). Like the old site they read the
// measured networks only.

const homeInsightRows = 5

// HomeInsights is the insight deck of one range.
type HomeInsights struct {
	Authenticity []HomePick         `json:"authenticity"`
	Hardest      *HomeHardest       `json:"hardest"`
	HardestRates []HomeTargetRate   `json:"hardestRates"`
	Exclusivity  []HomeExclusiveRow `json:"exclusivity"`
	ValueAll     HomeValueAll       `json:"valueAll"`
}

// HomeHardest is the measured target with the highest difficulty.
type HomeHardest struct {
	ID         string    `json:"id"`
	Difficulty Num       `json:"difficulty"`
	Engine     model.Str `json:"engine"`
}

// HomeTargetRate is a network's success on the hardest target; Engine is
// the browser engine of its tries (http on a network target) and Scores its
// success score (null when the snapshot has none).
type HomeTargetRate struct {
	Slug        string           `json:"slug"`
	Name        string           `json:"name"`
	SuccessRate Num              `json:"-"` // internal (SUCCESS-RATE-REMOVE-1, #900): Scores is served
	Engine      model.Str        `json:"engine"`
	Scores      *HomeTargetScore `json:"scores"`
}

// HomeTargetScore is the rank-based success score of a target.
type HomeTargetScore struct {
	Success model.MetricScore `json:"success"`
}

// HomeExclusiveRow is a pool's exclusive share (Value), its exclusive IPs,
// their score and the pool they are counted on (/v1/overlap's), and its
// likely upstream.
type HomeExclusiveRow struct {
	Slug           string             `json:"slug"`
	Name           string             `json:"name"`
	Value          Num                `json:"value"`
	ExclusiveIps   Num                `json:"exclusiveIps"`
	ExclusiveScore *model.MetricScore `json:"exclusiveScore"`
	PoolIps        Num                `json:"poolIps"`
	Upstream       *OverlapUpstream   `json:"upstream"`
}

func homeInsights(c *Context, all []RankingRow, rangeID string) HomeInsights {
	rows := []RankingRow{}
	for _, r := range all {
		if r.Source == model.SourceMeasured {
			rows = append(rows, r)
		}
	}
	ins := HomeInsights{Authenticity: []HomePick{}, HardestRates: []HomeTargetRate{},
		Exclusivity: []HomeExclusiveRow{}, ValueAll: homeValueAll(c, rows, rangeID)}
	for _, r := range topBy(rows, func(r RankingRow) Num { return r.Authenticity }, func(r RankingRow) bool { return r.Kind != "datacenter" }) {
		ins.Authenticity = append(ins.Authenticity, HomePick{Slug: r.Slug, Name: r.Name, Value: r.Authenticity})
	}
	if t := hardestTarget(c); t != nil {
		ins.Hardest = &HomeHardest{ID: t.ID, Difficulty: t.Difficulty, Engine: t.Engine}
		ins.HardestRates = hardestRates(c, rows, *t, rangeID)
	}
	ins.Exclusivity = homeExclusivity(c, rows, rangeID)
	return ins
}

// homeExclusivity is the top five rows by exclusiveScore, highest first,
// ties by name and a missing score last, each with its kind's overlap row
// in the range.
func homeExclusivity(c *Context, rows []RankingRow, rangeID string) []HomeExclusiveRow {
	overlap := map[string]map[string]OverlapNetwork{}
	out := []HomeExclusiveRow{}
	for _, r := range rows {
		if overlap[r.Kind] == nil {
			overlap[r.Kind] = map[string]OverlapNetwork{}
			for _, n := range OverlapFor(c, r.Kind, rangeID).Networks {
				overlap[r.Kind][n.Slug] = n
			}
		}
		row := HomeExclusiveRow{Slug: r.Slug, Name: r.Name, Value: r.ExclusiveShare, ExclusiveIps: nan, PoolIps: nan}
		if n, ok := overlap[r.Kind][r.Slug]; ok {
			row.ExclusiveIps, row.ExclusiveScore, row.PoolIps, row.Upstream = n.ExclusiveIps, n.ExclusiveScore, n.PoolIps, n.LikelyUpstream
		}
		out = append(out, row)
	}
	score := func(r HomeExclusiveRow) Num {
		if r.ExclusiveScore == nil {
			return nan
		}
		return r.ExclusiveScore.Score
	}
	sort.SliceStable(out, func(i, j int) bool {
		a, b := score(out[i]), score(out[j])
		if a.OK() && b.OK() && a == b || !a.OK() && !b.OK() {
			return out[i].Name < out[j].Name
		}
		return higherFirst(a, b)
	})
	return out[:min(len(out), homeInsightRows)]
}

// topBy is the first five rows that keep holds, by value highest first and
// missing values last (ties keep the rankings' order).
func topBy(rows []RankingRow, value func(RankingRow) Num, keep func(RankingRow) bool) []RankingRow {
	out := []RankingRow{}
	for _, r := range rows {
		if keep(r) {
			out = append(out, r)
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return higherFirst(value(out[i]), value(out[j])) })
	return out[:min(len(out), homeInsightRows)]
}

// higherFirst orders a before b when a is known and higher (or b missing).
func higherFirst(a, b Num) bool { return a.OK() && (!b.OK() || a > b) }

// hardestTarget is the measured target with the highest difficulty (the
// catalog's, else its scoring weight, as /v1/targets); the first in dataset
// order wins a tie. Nil when none has one.
func hardestTarget(c *Context) *model.TargetMeta {
	weights := scoreWeights(c.Snapshot)
	var hardest *model.TargetMeta
	for _, t := range c.Dataset.Targets {
		if t.Source != model.SourceMeasured {
			continue
		}
		if !t.Difficulty.OK() {
			if w, ok := weights[t.ID]; ok {
				t.Difficulty = w
			}
		}
		if t.Difficulty.OK() && (hardest == nil || t.Difficulty > hardest.Difficulty) {
			hardest = &t
		}
	}
	return hardest
}

// hardestRates is every network's success on t in the range, from its
// headline window, highest first; networks without one are left out.
func hardestRates(c *Context, rows []RankingRow, t model.TargetMeta, rangeID string) []HomeTargetRate {
	byslug := entriesBySlug(c.Entries)
	rates := []HomeTargetRate{}
	for _, r := range rows {
		e := byslug[r.Slug]
		if e == nil || e.Snap[e.P.PrimaryKind] == nil {
			continue
		}
		b := targetBenchmarks(e.Snap[e.P.PrimaryKind].Window(rangeID), []model.TargetMeta{t})[0]
		if !b.SuccessRate.OK() {
			continue
		}
		rate := HomeTargetRate{Slug: r.Slug, Name: r.Name, SuccessRate: b.SuccessRate, Engine: model.S(model.EngineHTTP)}
		if bb := b.BrowserBenchmark; bb != nil {
			rate.Engine = bb.Engine
		}
		if b.Scores != nil {
			rate.Scores = &HomeTargetScore{Success: b.Scores.Success.MetricScore}
		}
		rates = append(rates, rate)
	}
	sort.SliceStable(rates, func(i, j int) bool { return rates[i].SuccessRate > rates[j].SuccessRate })
	return rates
}
