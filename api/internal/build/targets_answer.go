package build

import (
	"errors"
	"net/http"
	"sort"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// TargetsAnswer is /v1/targets?kind=&range= (api/answers/targets.md): every
// target's meta and, for each network of one kind, every measured target's
// figures in one range, so a page draws the network x target matrix from one
// request. Rates, means and ranks are per engine: Chrome and Camoufox tries
// are never added together.
type TargetsAnswer struct {
	GeneratedAt string `json:"generatedAt"`
	Range       string `json:"range"`
	Kind        string `json:"kind"`
	// CardCount is how many targets are cards (TargetEntry.Card).
	CardCount int             `json:"cardCount"`
	Targets   []TargetEntry   `json:"targets"`
	Networks  []TargetNetwork `json:"networks"`
}

// TargetEntry is one target's meta (dataset order) with its fleet means and
// shared score status over the networks of the answer's kind.
type TargetEntry struct {
	ID       string    `json:"id"`
	Label    model.Str `json:"label"`
	Category model.Str `json:"category"`
	// Difficulty is the catalog's display difficulty, else ScoreWeight.
	Difficulty Num `json:"difficulty"`
	// ScoreWeight is the methodology's [browser_difficulty]: the target's
	// weight in the overall browser score (a group without one: its hardest source's).
	ScoreWeight   Num       `json:"scoreWeight"`
	PassCondition model.Str `json:"passCondition"`
	Engine        model.Str `json:"engine"`
	Group         model.Str `json:"group"`
	Sources       []string  `json:"sources"`
	// Card is a measured target that is no group's source.
	Card bool `json:"card"`
	// Fleet is, per engine, the mean success over the networks with tries.
	Fleet map[string]FleetMean `json:"fleet"`
	// ScoreStatus is, per engine, the success score status the networks share.
	ScoreStatus map[string]ScoreStatus `json:"scoreStatus"`
}

// FleetMean is the unweighted mean success of Networks networks.
type FleetMean struct {
	MeanSuccess Num `json:"meanSuccess"`
	Networks    int `json:"networks"`
}

// ScoreStatus is a target's success score status in one engine: "ranked",
// "all_similar" or "too_few_networks" (every network with enough tries
// shares it), else "not_enough_data"; RankedOf is null unless ranked.
type ScoreStatus struct {
	Status   string `json:"status"`
	RankedOf Num    `json:"rankedOf"`
}

// TargetNetwork is one network of the kind, in the rankings' order.
type TargetNetwork struct {
	Slug     string `json:"slug"`
	Name     string `json:"name"`
	Position int    `json:"position"`
	// Rank is the published rank among the ready networks of the kind.
	Rank           Num                             `json:"rank"`
	BrowserOverall map[string]model.BrowserOverall `json:"browserOverall"`
	// Cells is every measured target's figures, by target id.
	Cells map[string]*TargetCell `json:"cells"`
}

// TargetCell is one network's figures on one target in the range: a network
// page's targets[] entry (latency on generic_http only) without the copies of
// the main engine's figures, which are its engines.<engine>, and without a
// group's breakdown, which is its sources' own cells; with its cost and ranks.
type TargetCell struct {
	SuccessRate Num `json:"successRate"`
	CaptchaRate Num `json:"captchaRate"`
	BlockRate   Num `json:"blockRate"`
	Requests    Num `json:"requests"`
	LatencyP50  Num `json:"latencyP50"`
	LatencyP90  Num `json:"latencyP90"`
	// *BrowserCell is set exactly on browser targets.
	*BrowserCell
	// CostPer1kSuccess is the window cost rule on the target's own wire bytes;
	// null when the target has none.
	CostPer1kSuccess Num `json:"costPer1kSuccess"`
	// SuccessRank and CostRank rank the networks of the kind whose cell has
	// the same engine (higher success, lower cost first).
	SuccessRank model.Rank `json:"successRank"`
	CostRank    model.Rank `json:"costRank"`
}

// BrowserCell is a browser target's engines: Engine (the one with the most
// tries, whose figures the cell's rates are; null when none) and each
// engine's own figures, by name.
type BrowserCell struct {
	Engine  model.Str                        `json:"engine"`
	Engines map[string]model.EngineBenchmark `json:"engines"`
}

const notEnoughData = "not_enough_data"

func init() {
	RegisterAnswer(PageAnswer{Name: "targets", Route: "GET /v1/targets", Key: targetsKey, Build: buildTargets,
		Samples: []string{"/v1/targets", "/v1/targets?range=24h", "/v1/targets?kind=residential&range=7d", "/v1/targets?kind=datacenter"}})
}

func targetsPath(kind, rangeID string) string { return "/v1/targets/" + kind + "/" + rangeID }

func targetsKey(r *http.Request) (string, error) {
	q, err := Query(r, "kind", "range")
	if err != nil {
		return "", err
	}
	kind, err := QueryKind(q, "residential")
	if err != nil {
		return "", err
	}
	if !model.IsProxyKind(kind) {
		return "", BadRequest("kind must be " + strings.Join(model.ProxyKinds, ", "))
	}
	rangeID, err := QueryRange(q)
	return targetsPath(kind, rangeID), err
}

// buildTargets is one body per proxy kind and range.
func buildTargets(c *Context) (map[string]any, error) {
	if c.Dataset == nil {
		return nil, errors.New("no dataset")
	}
	weights := scoreWeights(c.Snapshot)
	measured := []model.TargetMeta{}
	for _, t := range c.Dataset.Targets {
		if t.Source == model.SourceMeasured {
			measured = append(measured, t)
		}
	}
	out := map[string]any{}
	for _, kind := range model.ProxyKinds {
		for _, r := range Ranges {
			out[targetsPath(kind, r.ID)] = targetsAnswer(c, weights, measured, kind, r.ID)
		}
	}
	return out, nil
}

// targetsAnswer is one kind's and range's body.
func targetsAnswer(c *Context, weights map[string]Num, measured []model.TargetMeta, kind, rangeID string) *TargetsAnswer {
	ans := &TargetsAnswer{GeneratedAt: c.GeneratedAt, Range: rangeID, Kind: kind, Targets: []TargetEntry{},
		Networks: targetNetworks(c.Entries, measured, kind, rangeID)}
	for _, t := range c.Dataset.Targets {
		te := targetEntry(t, weights, ans.Networks)
		if te.Card {
			ans.CardCount++
		}
		ans.Targets = append(ans.Targets, te)
	}
	return ans
}

// scoreWeights is each target's [browser_difficulty] in the snapshot's
// methodology, and each group without one its hardest source's
// (policy.browser_difficulty).
func scoreWeights(s *snap.Snapshot) map[string]Num {
	out := map[string]Num{}
	if s == nil {
		return out
	}
	for id, d := range s.Methodology.Settings.BrowserDifficulty {
		out[id] = Num(d)
	}
	for group, sources := range s.Methodology.Settings.TargetGroups {
		if _, ok := out[group]; ok {
			continue
		}
		for _, id := range sources {
			cur, has := out[group]
			if d, ok := s.Methodology.Settings.BrowserDifficulty[id]; ok && (!has || Num(d) > cur) {
				out[group] = Num(d)
			}
		}
	}
	return out
}

// targetNetworks is every network measured in kind, in the rankings' order
// (range score, highest first, then name), with its cells ranked.
func targetNetworks(entries []*entry, targets []model.TargetMeta, kind, rangeID string) []TargetNetwork {
	type ranked struct {
		row RankingRow
		net TargetNetwork
	}
	list := []ranked{}
	for _, e := range entries {
		sp := e.Snap[kind]
		if sp == nil {
			continue
		}
		w := sp.Window(rangeID)
		score := balancedScore(w)
		price := nan
		if pm := e.P.ProductMeasurements.Get(kind); pm != nil {
			price = listRate(pm.Pricing)
		}
		net := TargetNetwork{Slug: e.P.Slug, Name: e.P.Name, Rank: score.Rank, BrowserOverall: browserOverall(w),
			Cells: targetCells(w, targets, price)}
		list = append(list, ranked{RankingRow{Name: e.P.Name, Score: score.Score}, net})
	}
	sort.SliceStable(list, func(i, j int) bool { return rankingLess(list[i].row, list[j].row) })
	out := make([]TargetNetwork, len(list))
	for i := range list {
		out[i] = list[i].net
		out[i].Position = i + 1
	}
	for _, t := range targets {
		rankCells(out, t.ID)
	}
	return out
}

// targetCells is a network's cell of every measured target in window w, its
// cost at price ($ per GB).
func targetCells(w snap.Window, targets []model.TargetMeta, price Num) map[string]*TargetCell {
	cells := make(map[string]*TargetCell, len(targets))
	for _, b := range targetBenchmarks(w, targets) {
		cell := &TargetCell{SuccessRate: b.SuccessRate, CaptchaRate: b.CaptchaRate, BlockRate: b.BlockRate, Requests: b.Requests,
			LatencyP50: b.LatencyP50, LatencyP90: b.LatencyP90, CostPer1kSuccess: nan}
		if bb := b.BrowserBenchmark; bb != nil {
			cell.BrowserCell = &BrowserCell{Engine: bb.Engine, Engines: bb.Engines}
		}
		if m := w.Targets[b.Target].Metrics; (m.BytesUp + m.BytesDown) > 0 {
			cell.CostPer1kSuccess = wireCostPer1k(price, m)
		}
		cells[b.Target] = cell
	}
	return cells
}

// cellEngine is the engine a cell's rates are of: http on a network target.
func cellEngine(c *TargetCell) string {
	if c.BrowserCell == nil {
		return model.EngineHTTP
	}
	return model.SV(c.Engine)
}

// rankCells ranks one target's cells by success and cost among the cells of
// the same engine.
func rankCells(nets []TargetNetwork, id string) {
	success, cost := map[string][]Num{}, map[string][]Num{}
	for _, n := range nets {
		c, engine := n.Cells[id], cellEngine(n.Cells[id])
		if c.SuccessRate.OK() {
			success[engine] = append(success[engine], c.SuccessRate)
		}
		if c.CostPer1kSuccess.OK() {
			cost[engine] = append(cost[engine], c.CostPer1kSuccess)
		}
	}
	for _, n := range nets {
		c, engine := n.Cells[id], cellEngine(n.Cells[id])
		c.SuccessRank = rankOf(c.SuccessRate, success[engine], true)
		c.CostRank = rankOf(c.CostPer1kSuccess, cost[engine], false)
	}
}

// targetEntry is a target's meta, fleet means and score status.
func targetEntry(t model.TargetMeta, weights map[string]Num, nets []TargetNetwork) TargetEntry {
	weight, ok := weights[t.ID]
	if !ok {
		weight = nan
	}
	te := TargetEntry{ID: t.ID, Label: t.Label, Category: t.Category, Difficulty: t.Difficulty, ScoreWeight: weight,
		PassCondition: t.PassCondition, Engine: t.Engine, Group: t.Group, Sources: t.Sources,
		Card: t.Group == nil && t.Source == model.SourceMeasured}
	if !te.Difficulty.OK() {
		te.Difficulty = weight
	}
	if te.Sources == nil {
		te.Sources = []string{}
	}
	te.Fleet, te.ScoreStatus = fleetOf(t.ID, nets)
	return te
}

// fleetOf is a target's mean success per engine over the networks with tries
// of it (an engine's successes / tries on a browser target, the success rate
// on a network target, whose engine is http) and each engine's shared score status.
func fleetOf(id string, nets []TargetNetwork) (map[string]FleetMean, map[string]ScoreStatus) {
	fleet, status, sum := map[string]FleetMean{}, map[string]ScoreStatus{}, map[string]float64{}
	add := func(engine string, requests, rate Num) {
		if requests > 0 && rate.OK() {
			f := fleet[engine]
			f.Networks++
			fleet[engine], sum[engine] = f, sum[engine]+rate.F()
		}
	}
	for _, n := range nets {
		c := n.Cells[id]
		switch {
		case c == nil:
		case c.BrowserCell == nil:
			add(model.EngineHTTP, c.Requests, c.SuccessRate)
		default:
			for name, e := range c.Engines {
				add(name, e.Requests, e.Successes/e.Requests)
				shareStatus(status, name, e.Scores)
			}
		}
	}
	for engine, f := range fleet {
		f.MeanSuccess = Num(sum[engine] / float64(f.Networks))
		fleet[engine] = f
	}
	return fleet, status
}

// shareStatus keeps an engine's target-level status: the first network's,
// replaced by the first one ranked for the target (a status that is not
// not_enough_data, which every such network shares).
// Scores nil (a snapshot without scores) leaves it unchanged.
func shareStatus(status map[string]ScoreStatus, engine string, scores *model.TargetScores) {
	if scores == nil {
		return
	}
	m := scores.Success
	if cur, seen := status[engine]; !seen || cur.Status == notEnoughData && m.Status != notEnoughData {
		status[engine] = ScoreStatus{Status: m.Status, RankedOf: m.RankedOf}
	}
}
