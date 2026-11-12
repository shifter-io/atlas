package build

import (
	"slices"
	"sort"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// RankingsAnswer is /v1/rankings?range=&kind=: every network (of one kind
// with kind) in its final order for one range, with every figure the list
// pages draw, so a page renders its list at once and needs no request per
// network.
type RankingsAnswer struct {
	GeneratedAt string                 `json:"generatedAt"`
	Range       string                 `json:"range"`
	Profile     string                 `json:"profile"`
	Targets     TargetCounts           `json:"targets"`
	Weights     model.Breakdown        `json:"weights"`
	Profiles    []model.RankingProfile `json:"profiles"`
	Fleet       RankingFleet           `json:"fleet"`
	Cost        RankingsCost           `json:"cost"`
	Networks    []RankingRow           `json:"networks"`
	// ScoreDefinitions is the 1-10 scores' tooltips.
	ScoreDefinitions ScoreDefinitions `json:"scoreDefinitions"`
	// NAList lists the answer's own nulls (cost, profiles); each row has its own.
	NAList
}

// RankingRow is one network of a ranking. Position is its place in the list
// (1 = first): measured networks by their range score, highest first, then the
// unscored ones by name. Rank is the published rank among the ready networks
// of the same kind (Peers of them), so two kinds can each have a #1.
type RankingRow struct {
	Position     int             `json:"position"`
	Slug         string          `json:"slug"`
	Name         string          `json:"name"`
	Source       string          `json:"source"`
	Kind         string          `json:"kind"`
	Kinds        []string        `json:"kinds"`
	Tagline      model.Str       `json:"tagline"`
	Rank         Num             `json:"rank"`
	Peers        Num             `json:"peers"`
	Standing     Standing        `json:"standing"`
	PreviousRank Num             `json:"previousRank"`
	Score        Num             `json:"score"`
	Grade        model.Str       `json:"grade"`
	Breakdown    model.Breakdown `json:"breakdown"`
	SuccessRate  Num             `json:"successRate"`
	SuccessDelta Num             `json:"successDeltaPts"`
	LatencyP50   Num             `json:"latencyP50"`
	LatencyP90   Num             `json:"latencyP90"`
	LatencyP99   Num             `json:"latencyP99"`
	LatencyDelta Num             `json:"latencyDeltaPct"`
	UniqueIps    Num             `json:"uniqueIps"`
	Requests     Num             `json:"requests"`
	UptimePct    Num             `json:"uptimePct"`
	Downtime     Num             `json:"downtimeMinutes"`
	CostPer1k    Num             `json:"costPer1kSuccess"`
	ValueRank    *int            `json:"valueRank"`
	// ValueRankAllTargets ranks costPer1kAllTargets (rankAllTargets).
	ValueRankAllTargets *int `json:"valueRankAllTargets"`
	RowPricing
	RowTrust
	WindowFigures
	// Trend is the range's success rate history (WindowStats.success) thinned
	// to at most TrendPoints values: the list's sparkline; TrendT is each
	// point's time in minutes before the build.
	Trend  []Num `json:"trend"`
	TrendT []Num `json:"trendT"`
	// BrowserOverall is the range's overall browser success score per engine;
	// nil (null) when the range has none.
	BrowserOverall map[string]RowBrowserScore `json:"browserOverall"`
	NAList
	// successes is the range's successful requests, for the answer's fleet.
	successes Num
	// why is the reason codes of the range score's nulls.
	why scoreWhy
}

// TrendPoints is how many success values a ranking row's trend carries.
const TrendPoints = 26

// RankingProfile is the ranking profile the rankings use (the snapshot's
// headline weighting, as the dataset's score).
const RankingProfile = "balanced"

// rankingSource is one network of a ranking: its provider view and snapshot
// group of the kind shown and that kind's window statistics by range.
type rankingSource struct {
	e     *entry
	p     *model.Provider
	kind  string
	sp    *snap.Product
	stats map[string]*WindowStats
}

// rankings orders every network for each range (by range), and the networks
// of each proxy kind for each range (by range + "/" + kind).
func rankings(ds *model.Dataset, entries []*entry, stats map[string]map[string]*WindowStats,
	details map[string]*ProviderDetail) (all, byKind map[string]*RankingsAnswer) {
	base := RankingsAnswer{GeneratedAt: ds.GeneratedAt, Profile: RankingProfile, Targets: targetCounts(ds.Targets),
		Weights: ds.Weights, Profiles: ds.Profiles, ScoreDefinitions: scoreDefinitions}
	all, byKind = map[string]*RankingsAnswer{}, map[string]*RankingsAnswer{}
	every := rankingSources(entries, stats, details, "")
	for _, r := range Ranges {
		all[r.ID] = rankingAnswer(base, every, r.ID)
	}
	for _, kind := range model.ProxyKinds {
		srcs := rankingSources(entries, stats, details, kind)
		for _, r := range Ranges {
			byKind[r.ID+"/"+kind] = rankingAnswer(base, srcs, r.ID)
		}
	}
	return all, byKind
}

// rankingSources is every network with its headline group (kind ""), or the
// networks that list kind with that kind's group.
func rankingSources(entries []*entry, stats map[string]map[string]*WindowStats, details map[string]*ProviderDetail,
	kind string) []rankingSource {
	out := []rankingSource{}
	for _, e := range entries {
		slug := e.P.Slug
		if kind == "" {
			out = append(out, rankingSource{e: e, p: e.P, kind: e.P.PrimaryKind, sp: e.Snap[e.P.PrimaryKind], stats: stats[slug]})
			continue
		}
		if !slices.Contains(networkRow(e).Kinds, kind) {
			continue
		}
		src := rankingSource{e: e, p: e.P, kind: kind, sp: e.Snap[kind], stats: stats[slug]}
		if view := providerForProduct(e.P, kind); view != nil {
			src.p = view
		}
		if d := details[slug]; d != nil && d.Windows[kind] != nil {
			src.stats = d.Windows[kind]
		}
		out = append(out, src)
	}
	return out
}

// rankingAnswer is one range's ranking of srcs.
func rankingAnswer(base RankingsAnswer, srcs []rankingSource, rangeID string) *RankingsAnswer {
	ans := base
	ans.Range, ans.Networks = rangeID, []RankingRow{}
	ranks := valueRanks(srcs, rangeID)
	for _, src := range srcs {
		ans.Networks = append(ans.Networks, rankingRow(src, rangeID, ranks))
	}
	sort.SliceStable(ans.Networks, func(i, j int) bool { return rankingLess(ans.Networks[i], ans.Networks[j]) })
	for i := range ans.Networks {
		ans.Networks[i].Position = i + 1
	}
	rankAllTargets(ans.Networks)
	ans.Fleet = rankingFleet(ans.Networks)
	ans.Cost = RankingsCost{CostSummary: costSummary(ans.Networks, func(r RankingRow) Num { return r.CostPer1k }),
		AllTargets: costSummary(ans.Networks, func(r RankingRow) Num { return r.CostPer1kAllTargets })}
	return &ans
}

func rankingRow(src rankingSource, rangeID string, ranks map[string]int) RankingRow {
	p, s := src.p, src.stats[rangeID]
	row := RankingRow{Slug: p.Slug, Name: p.Name, Source: p.Source, Kind: src.kind, Kinds: networkRow(src.e).Kinds, Tagline: p.Tagline,
		Rank: p.Score.Rank, Peers: nan, PreviousRank: p.Score.PreviousRank, Score: p.Score.Composite, Grade: p.Score.Grade,
		Breakdown: p.Score.Breakdown, SuccessRate: s.SuccessRate, SuccessDelta: s.SuccessDeltaPts, LatencyP50: s.LatencyP50,
		LatencyP90: s.LatencyP90, LatencyP99: s.LatencyP99, LatencyDelta: s.LatencyDeltaPct, UniqueIps: s.UniqueIps,
		Requests: s.Requests, UptimePct: s.UptimePct, Downtime: s.DowntimeMinutes, CostPer1k: s.CostPer1kSuccess,
		RowPricing: rowPricing(p.Pricing), RowTrust: rowTrust(p.Trust), WindowFigures: s.WindowFigures,
		Trend: []Num{}, TrendT: []Num{}, successes: nan}
	// A measured network's score, grade and rank are its group's for this range.
	if sp := src.sp; sp != nil {
		w := sp.Window(rangeID)
		score, ok := w.Scores[RankingProfile]
		row.Score, row.Grade, row.Rank, row.PreviousRank, row.Peers = nan, nil, nan, nan, nan
		row.Breakdown = model.NewBreakdown()
		if ok {
			row.Score, row.Grade, row.Rank, row.PreviousRank, row.Peers = score.Score, score.Grade, score.Rank, score.PreviousRank, score.Peers()
			row.why = whyOf(score)
			for k, c := range score.Components {
				row.Breakdown.Set(k, c.Value)
			}
		}
		row.successes, row.BrowserOverall = w.Metrics.Successes, rowBrowser(w)
	}
	row.Standing = Standing{Rank: model.StandingRank(row.Rank, row.Score, row.Peers), Peers: row.Peers}
	if v, ok := ranks[p.Slug]; ok {
		row.ValueRank = &v
	}
	for _, pt := range downsample(s.Success, TrendPoints) {
		row.Trend, row.TrendT = append(row.Trend, pt.V), append(row.TrendT, pt.T)
	}
	return row
}

// rankingLess orders by score (highest first, unscored last), then name.
func rankingLess(a, b RankingRow) bool {
	if a.Score.OK() != b.Score.OK() {
		return a.Score.OK()
	}
	if a.Score.OK() && a.Score != b.Score {
		return a.Score > b.Score
	}
	return a.Name < b.Name
}
