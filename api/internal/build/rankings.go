package build

import (
	"sort"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

// RankingsAnswer is /v1/rankings?range=: every network in its final order for
// one range, with what the overview list prints, so a page renders the list
// at once instead of reordering it row by row.
type RankingsAnswer struct {
	GeneratedAt string       `json:"generatedAt"`
	Range       string       `json:"range"`
	Profile     string       `json:"profile"`
	Networks    []RankingRow `json:"networks"`
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
	Rank         Num             `json:"rank"`
	Peers        Num             `json:"peers"`
	PreviousRank Num             `json:"previousRank"`
	Score        Num             `json:"score"`
	Grade        model.Str       `json:"grade"`
	Breakdown    model.Breakdown `json:"breakdown"`
	SuccessRate  Num             `json:"successRate"`
	SuccessDelta Num             `json:"successDeltaPts"`
	LatencyP50   Num             `json:"latencyP50"`
	LatencyDelta Num             `json:"latencyDeltaPct"`
	UniqueIps    Num             `json:"uniqueIps"`
	Countries    Num             `json:"countries"`
	HostingShare Num             `json:"datacenterAsnShare"`
	CostPer1k    Num             `json:"costPer1kSuccess"`
	ValueRank    *int            `json:"valueRank"`
	// Trend is the range's success rate history (WindowStats.success) thinned
	// to at most TrendPoints values: the list's sparkline.
	Trend []Num `json:"trend"`
}

// TrendPoints is how many success values a ranking row's trend carries.
const TrendPoints = 26

// RankingProfile is the ranking profile the rankings use (the snapshot's
// headline weighting, as the dataset's score).
const RankingProfile = "balanced"

// rankings orders every network for each range.
func rankings(generatedAt string, entries []*entry, stats map[string]map[string]*WindowStats) map[string]*RankingsAnswer {
	out := map[string]*RankingsAnswer{}
	for _, r := range Ranges {
		board := costBoard(entries, stats, r.ID)
		ans := &RankingsAnswer{GeneratedAt: generatedAt, Range: r.ID, Profile: RankingProfile, Networks: []RankingRow{}}
		for _, e := range entries {
			ans.Networks = append(ans.Networks, rankingRow(e, stats[e.P.Slug][r.ID], r.ID, board))
		}
		sort.SliceStable(ans.Networks, func(i, j int) bool { return rankingLess(ans.Networks[i], ans.Networks[j]) })
		for i := range ans.Networks {
			ans.Networks[i].Position = i + 1
		}
		out[r.ID] = ans
	}
	return out
}

func rankingRow(e *entry, s *WindowStats, rangeID string, board CostBoard) RankingRow {
	p := e.P
	row := RankingRow{Slug: p.Slug, Name: p.Name, Source: p.Source, Kind: p.PrimaryKind, Kinds: networkRow(e).Kinds,
		Rank: p.Score.Rank, Peers: nan, PreviousRank: p.Score.PreviousRank, Score: p.Score.Composite, Grade: p.Score.Grade,
		Breakdown: p.Score.Breakdown, SuccessRate: s.SuccessRate, SuccessDelta: s.SuccessDeltaPts, LatencyP50: s.LatencyP50,
		LatencyDelta: s.LatencyDeltaPct, UniqueIps: s.UniqueIps, Countries: p.Pool.Countries, HostingShare: p.Trust.DatacenterAsnShare,
		CostPer1k: s.CostPer1kSuccess, Trend: []Num{}}
	// A measured network's score, grade and rank are its headline group's for this range.
	if sp := e.Snap[p.PrimaryKind]; sp != nil {
		score, ok := sp.Window(rangeID).Scores[RankingProfile]
		row.Score, row.Grade, row.Rank, row.PreviousRank, row.Peers = nan, nil, nan, nan, nan
		row.Breakdown = model.NewBreakdown()
		if ok {
			row.Score, row.Grade, row.Rank, row.PreviousRank, row.Peers = score.Score, score.Grade, score.Rank, score.PreviousRank, score.Peers()
			for k, c := range score.Components {
				row.Breakdown.Set(k, c.Value)
			}
		}
	}
	if v, ok := board.Ranks[p.Slug]; ok {
		row.ValueRank = &v
	}
	for _, pt := range downsample(s.Success, TrendPoints) {
		row.Trend = append(row.Trend, pt.V)
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
