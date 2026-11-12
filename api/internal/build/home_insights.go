package build

import (
	"sort"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

// The home's four insight cards (api/answers/home.md), ported from the old
// site's overview.ts overviewInsights over the range's ranking rows (W2), the
// target benchmarks and the overlap (W5). Like the old site they read the
// measured networks only.

const homeInsightRows = 5

// HomeInsights is the insight deck of one range.
type HomeInsights struct {
	Value        HomeValue          `json:"value"`
	Authenticity []HomePick         `json:"authenticity"`
	Hardest      *HomeHardest       `json:"hardest"`
	HardestRates []HomeTargetRate   `json:"hardestRates"`
	Exclusivity  []HomeExclusiveRow `json:"exclusivity"`
	ValueAll     HomeValueAll       `json:"valueAll"`
}

// HomeBill is a residential network's bill for one million successes.
type HomeBill struct {
	Slug string `json:"slug"`
	Name string `json:"name"`
	Bill Num    `json:"bill"`
}

// HomeValue is every residential network's bill, cheapest first and
// unpriced last; best and worst are the cheapest and dearest priced ones,
// Unpriced counts the others.
type HomeValue struct {
	Best     *HomeBill  `json:"best"`
	Worst    *HomeBill  `json:"worst"`
	SpreadX  Num        `json:"spreadX"`
	Unpriced int        `json:"unpriced"`
	Rows     []HomeBill `json:"rows"`
}

// HomeHardest is the measured target with the highest difficulty.
type HomeHardest struct {
	ID            string    `json:"id"`
	Label         model.Str `json:"label"`
	Difficulty    Num       `json:"difficulty"`
	PassCondition model.Str `json:"passCondition"`
	Engine        model.Str `json:"engine"`
}

// HomeTargetRate is a network's success on the hardest target; Engine is
// the browser engine of its tries (http on a network target) and Scores its
// success score (null when the snapshot has none).
type HomeTargetRate struct {
	Slug        string           `json:"slug"`
	Name        string           `json:"name"`
	SuccessRate Num              `json:"successRate"`
	Engine      model.Str        `json:"engine"`
	Scores      *HomeTargetScore `json:"scores"`
}

// HomeTargetScore is the rank-based success score of a target.
type HomeTargetScore struct {
	Success model.MetricScore `json:"success"`
}

// HomeExclusiveRow is a pool's exclusive share and its likely upstream.
type HomeExclusiveRow struct {
	Slug     string           `json:"slug"`
	Name     string           `json:"name"`
	Value    Num              `json:"value"`
	Upstream *OverlapUpstream `json:"upstream"`
}

func homeInsights(c *Context, all []RankingRow, rangeID string) HomeInsights {
	rows := []RankingRow{}
	for _, r := range all {
		if r.Source == model.SourceMeasured {
			rows = append(rows, r)
		}
	}
	ins := HomeInsights{Value: homeValue(rows), Authenticity: []HomePick{}, HardestRates: []HomeTargetRate{},
		Exclusivity: []HomeExclusiveRow{}, ValueAll: homeValueAll(c, rows, rangeID)}
	for _, r := range topBy(rows, func(r RankingRow) Num { return r.Authenticity }, func(r RankingRow) bool { return r.Kind != "datacenter" }) {
		ins.Authenticity = append(ins.Authenticity, HomePick{Slug: r.Slug, Name: r.Name, Value: r.Authenticity})
	}
	if t := hardestTarget(c); t != nil {
		ins.Hardest = &HomeHardest{ID: t.ID, Label: t.Label, Difficulty: t.Difficulty, PassCondition: t.PassCondition, Engine: t.Engine}
		ins.HardestRates = hardestRates(c, rows, *t, rangeID)
	}
	upstreams := map[string]map[string]*OverlapUpstream{}
	for _, r := range topBy(rows, func(r RankingRow) Num { return r.ExclusiveShare }, func(RankingRow) bool { return true }) {
		if upstreams[r.Kind] == nil {
			upstreams[r.Kind] = map[string]*OverlapUpstream{}
			for _, n := range OverlapFor(c, r.Kind, rangeID).Networks {
				upstreams[r.Kind][n.Slug] = n.LikelyUpstream
			}
		}
		ins.Exclusivity = append(ins.Exclusivity, HomeExclusiveRow{Slug: r.Slug, Name: r.Name, Value: r.ExclusiveShare,
			Upstream: upstreams[r.Kind][r.Slug]})
	}
	return ins
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

// homeValue is the residential rows' bills (cost per 1k successes × 1000).
func homeValue(rows []RankingRow) HomeValue {
	v := HomeValue{SpreadX: nan, Rows: []HomeBill{}}
	for _, r := range rows {
		if r.Kind == "residential" {
			v.Rows = append(v.Rows, HomeBill{Slug: r.Slug, Name: r.Name, Bill: r.CostPer1k * 1000})
		}
	}
	sort.SliceStable(v.Rows, func(i, j int) bool { return missingLast(v.Rows[i].Bill, v.Rows[j].Bill) < 0 })
	for i := range v.Rows {
		b := v.Rows[i]
		if !b.Bill.OK() {
			v.Unpriced++
			continue
		}
		if v.Best == nil {
			v.Best = &b
		}
		v.Worst = &b
	}
	if v.Best != nil {
		v.SpreadX = ratio(v.Worst.Bill, v.Best.Bill)
	}
	return v
}

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
			rate.Scores = &HomeTargetScore{Success: b.Scores.Success}
		}
		rates = append(rates, rate)
	}
	sort.SliceStable(rates, func(i, j int) bool { return rates[i].SuccessRate > rates[j].SuccessRate })
	return rates
}
