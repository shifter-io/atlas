package build

import (
	"sort"

	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// insights.valueAll (api/answers/home.md, VALUE-ALL-1): each measured
// residential network's bill per 1,000 successful requests, the plain mean
// over the card targets of each target's cost: every wire byte of its
// finished tries (failed ones too) at the list rate, ÷ its successes.
// Unrounded, at GB = 10^9 bytes, the GB networks sell (traffic.py).

const (
	gbBytes = 1e9
	// valueLowSuccesses: a counted target below this many successes is low-sample.
	valueLowSuccesses = 20
	valueAllKind      = "residential"
)

// TargetCost is one target's cost per 1,000 successes in a window, with the
// tries, successes and MB per try it comes from.
type TargetCost struct {
	CostPer1k Num `json:"costPer1k"`
	Tries     int `json:"tries"`
	Successes int `json:"successes"`
	MbPerTry  Num `json:"mbPerTry"`
}

// ValueAllPick is the network a valueAll summary names, with its bill.
type ValueAllPick struct {
	Slug      string `json:"slug"`
	Name      string `json:"name"`
	CostPer1k Num    `json:"costPer1k"`
}

// ValueAllRow is one network's 8-target bill and each target's cost: the
// bill, partial bill, counted and missing targets its range's value
// component publishes (VALUE-ALL-RANK-1; CostIsLowerBound when it is a lower
// bound), else billOver's. ByTarget and LowSampleTargets are billOver's.
type ValueAllRow struct {
	Slug             string                `json:"slug"`
	Name             string                `json:"name"`
	CostPer1k        Num                   `json:"costPer1k"`
	PartialCostPer1k Num                   `json:"partialCostPer1k"`
	TargetsCounted   int                   `json:"targetsCounted"`
	MissingTargets   []string              `json:"missingTargets"`
	CostIsLowerBound bool                  `json:"costIsLowerBound"`
	LowSampleTargets []string              `json:"lowSampleTargets"`
	ByTarget         map[string]TargetCost `json:"byTarget"`
	NAList
	priced bool
}

// HomeValueAll is every row cheapest first: complete priced rows by bill (a
// lower bound counted as one), then incomplete priced rows (most targets
// counted first, then partial bill), then unpriced rows.
type HomeValueAll struct {
	Unit       string        `json:"unit"`
	GbBytes    int64         `json:"gbBytes"`
	Weighting  string        `json:"weighting"`
	Targets    []string      `json:"targets"`
	Best       *ValueAllPick `json:"best"`
	Worst      *ValueAllPick `json:"worst"`
	SpreadX    Num           `json:"spreadX"`
	Incomplete int           `json:"incomplete"`
	Unpriced   int           `json:"unpriced"`
	Rows       []ValueAllRow `json:"rows"`
}

// targetCost is one target's cost at price ($ per GB) from its window
// metrics m, and the reason when the cost is null.
func targetCost(price Num, m snap.Metrics) (TargetCost, string) {
	tc := TargetCost{CostPer1k: nan, MbPerTry: nan, Tries: countOf(m.Completed), Successes: countOf(m.Successes)}
	bytes := m.BytesUp + m.BytesDown
	measured := bytes.OK() && bytes > 0
	if tc.Tries > 0 && measured {
		tc.MbPerTry = bytes / Num(tc.Tries) / 1e6
	}
	switch {
	case tc.Tries == 0:
		return tc, NANoRun
	case tc.Successes == 0:
		return tc, NANoSuccesses
	case !measured:
		return tc, NANotMeasured
	case !price.OK():
		return tc, NACatalog
	}
	tc.CostPer1k = price * bytes / gbBytes / Num(tc.Successes) * 1000
	return tc, ""
}

// countOf is a metric's count, 0 when missing.
func countOf(n Num) int {
	if !n.OK() || n < 0 {
		return 0
	}
	return int(n)
}

// homeValueAll is valueAll over the measured residential rows (in order).
func homeValueAll(c *Context, rows []RankingRow, rangeID string) HomeValueAll {
	v := HomeValueAll{Unit: "usd_per_1k_successes", GbBytes: gbBytes, Weighting: "equal", Targets: []string{},
		SpreadX: nan, Rows: []ValueAllRow{}}
	for _, t := range c.Dataset.Targets {
		if isCard(t) {
			v.Targets = append(v.Targets, t.ID)
		}
	}
	byslug := entriesBySlug(c.Entries)
	for _, r := range rows {
		if r.Kind == valueAllKind {
			v.Rows = append(v.Rows, valueAllRow(r, byslug[r.Slug], v.Targets, rangeID))
		}
	}
	sort.SliceStable(v.Rows, func(i, j int) bool { return valueAllLess(&v.Rows[i], &v.Rows[j]) })
	for i := range v.Rows {
		r := &v.Rows[i]
		switch r.tier() {
		case 0:
			pick := &ValueAllPick{Slug: r.Slug, Name: r.Name, CostPer1k: r.CostPer1k}
			if v.Best == nil {
				v.Best = pick
			}
			v.Worst = pick
		case 1:
			v.Incomplete++
		default:
			v.Unpriced++
		}
	}
	if v.Best != nil {
		v.SpreadX = ratio(v.Worst.CostPer1k, v.Best.CostPer1k)
	}
	return v
}

// valueAllRow is r's bill over targets from e's residential window.
func valueAllRow(r RankingRow, e *entry, targets []string, rangeID string) ValueAllRow {
	price, w, value := nan, snap.EmptyWindow(), snap.Component{}
	if e != nil {
		if pm := e.P.ProductMeasurements.Get(valueAllKind); pm != nil {
			price = listRate(pm.Pricing)
		}
		if sp := e.Snap[valueAllKind]; sp != nil {
			w, value = sp.Window(rangeID), rangeValue(sp, rangeID)
		}
	}
	b := billOver(price, w, targets)
	row := ValueAllRow{Slug: r.Slug, Name: r.Name, CostPer1k: b.bill, PartialCostPer1k: b.partial, TargetsCounted: b.counted,
		MissingTargets: b.missing, LowSampleTargets: b.low, ByTarget: b.byTarget, priced: price.OK()}
	if a, ok := value.AllTargets(); ok {
		row.CostPer1k, row.PartialCostPer1k, row.TargetsCounted = a.Cost, a.Partial, a.Counted
		row.MissingTargets, row.CostIsLowerBound = a.Missing, a.LowerBound
	}
	row.listNA(targets, b.reasons)
	return row
}

// cardsBill is a window's bill per 1,000 successes over the card targets at
// a price: each target's cost and the reason it is null, the bill (every
// target counted), the partial bill (some, not all), and the targets missing
// a cost for a measured reason (a catalog reason is the price's, not theirs).
type cardsBill struct {
	bill, partial Num
	counted       int
	missing, low  []string
	byTarget      map[string]TargetCost
	reasons       map[string]string
}

// billOver is window w's bill over targets at price, the plain mean of
// their costs: insights.valueAll's and the rankings' (setAllTargets) both.
func billOver(price Num, w snap.Window, targets []string) cardsBill {
	b := cardsBill{bill: nan, partial: nan, missing: []string{}, low: []string{},
		byTarget: make(map[string]TargetCost, len(targets)), reasons: map[string]string{}}
	sum := 0.0
	for _, id := range targets {
		tc, why := targetCost(price, w.Targets[id].Metrics)
		b.byTarget[id], b.reasons[id] = tc, why
		switch {
		case why == "":
			b.counted++
			sum += tc.CostPer1k.F()
			if tc.Successes < valueLowSuccesses {
				b.low = append(b.low, id)
			}
		case why != NACatalog:
			b.missing = append(b.missing, id)
		}
	}
	switch n := b.counted; {
	case n > 0 && n == len(targets):
		b.bill = Num(sum / float64(n))
	case n > 0:
		b.partial = Num(sum / float64(n))
	}
	return b
}

// listNA lists the row's nulls: its bill (catalog without a price, else
// incomplete_targets) and each target's cost and MB per try with their own
// reason. partialCostPer1k is null by contract (a complete bill, or no
// target counted).
func (r *ValueAllRow) listNA(targets []string, reasons map[string]string) {
	codes := map[string]string{}
	if !r.CostPer1k.OK() {
		codes["costPer1k"] = NAIncompleteTargets
		if !r.priced {
			codes["costPer1k"] = NACatalog
		}
	}
	for _, id := range targets {
		tc, at := r.ByTarget[id], "byTarget."+id+"."
		if why := reasons[id]; why != "" {
			codes[at+"costPer1k"] = why
		}
		if !tc.MbPerTry.OK() {
			codes[at+"mbPerTry"] = NANotMeasured
			if tc.Tries == 0 {
				codes[at+"mbPerTry"] = NANoRun
			}
		}
	}
	paths := make([]string, 0, len(codes))
	for p := range codes {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	list := make([]string, len(paths))
	for i, p := range paths {
		list[i] = codes[p]
	}
	r.NAList.set(paths, list)
}

// tier is the row's sort group: 0 complete and priced, 1 priced but
// missing a target, 2 unpriced.
func (r *ValueAllRow) tier() int {
	switch {
	case !r.priced:
		return 2
	case !r.CostPer1k.OK():
		return 1
	}
	return 0
}

func valueAllLess(a, b *ValueAllRow) bool {
	if ta, tb := a.tier(), b.tier(); ta != tb {
		return ta < tb
	}
	switch {
	case a.tier() == 0:
		return a.CostPer1k < b.CostPer1k
	case a.tier() == 1 && a.TargetsCounted != b.TargetsCounted:
		return a.TargetsCounted > b.TargetsCounted
	case a.tier() == 1:
		return missingLast(a.PartialCostPer1k, b.PartialCostPer1k) < 0
	}
	return false
}

// naCode is the reason of a null valueAll summary field (best, worst,
// spreadX): no row, every row unpriced, no complete priced row, or a best
// bill of 0.
func (v *HomeValueAll) naCode() string {
	switch {
	case len(v.Rows) == 0:
		return NANoRun
	case v.Unpriced == len(v.Rows):
		return NACatalog
	case v.Best == nil:
		return NAIncompleteTargets
	}
	return NAInsufficient
}
