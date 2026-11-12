package build

import (
	"errors"
	"math"
	"net/http"
	"slices"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// /v1/home?range= (api/answers/home.md): the Proxy Index home's live
// dashboard in one body per range: the market panel, the insight cards,
// every network's panel with its curve and sparks, and each tab's newest tape rows.

func homePath(rangeID string) string { return "/v1/home/" + rangeID }

func init() {
	RegisterAnswer(PageAnswer{Name: "home", Route: "GET /v1/home", Build: buildHome, InDataset: true,
		Request: func(p string) string { return queryRequest(p, "/v1/home", "range") },
		Samples: []string{"/v1/home", "/v1/home?range=24h", "/v1/home?range=7d"},
		Key: func(r *http.Request) (string, error) {
			q, err := Query(r, "range")
			if err != nil {
				return "", err
			}
			rangeID, err := QueryRange(q)
			return homePath(rangeID), err
		}})
}

// Point counts of the old site's home sparklines (export-home.mts).
const (
	homeCurve24h     = 84 // a network's 24h curve; other ranges keep every point
	homeNetworkSpark = 22
)

// HomeAnswer is one range of /v1/home.
type HomeAnswer struct {
	GeneratedAt string        `json:"generatedAt"`
	Range       string        `json:"range"`
	Market      HomeMarket    `json:"market"`
	Insights    HomeInsights  `json:"insights"`
	Networks    []HomeNetwork `json:"networks"`
	// Labels is every distinct label list of the networks' series, once: a
	// series' labels is its index here.
	Labels   [][]string             `json:"labels"`
	Tape     []model.TapeEvent      `json:"tape"`
	Profiles []model.RankingProfile `json:"profiles"`
	// ScoreDefinitions is the 1-10 scores' tooltips.
	ScoreDefinitions ScoreDefinitions `json:"scoreDefinitions"`
	// NAList lists the market's, the insights' and empty profiles' nulls; each
	// network, tape row and profile has its own.
	NAList
}

// HomePick is a network a market card names, with its figure.
type HomePick struct {
	Slug  string `json:"slug"`
	Name  string `json:"name"`
	Value Num    `json:"value"`
}

// HomeRef names a network.
type HomeRef struct {
	Slug string `json:"slug"`
	Name string `json:"name"`
}

// HomeSpread is the dearest residential bill for one million successes ÷
// the cheapest (unpriced networks left out), and both networks.
type HomeSpread struct {
	Value Num      `json:"value"`
	Best  *HomeRef `json:"best"`
	Worst *HomeRef `json:"worst"`
}

// HomeMarket is the All market panel of one range.
type HomeMarket struct {
	Requests      Num         `json:"requests"`
	ObservedDays  Num         `json:"observedDays"`
	Networks      int         `json:"networks"`
	Targets       int         `json:"targets"`
	Curve         SparkSeries `json:"curve"`
	MedianSuccess Num         `json:"medianSuccess"`
	// SuccessScore is MedianSuccess on the 1-10 scale (marketScore).
	SuccessScore  MarketScore `json:"successScore"`
	UniqueIps     Num         `json:"uniqueIps"`
	BandwidthGb   Num         `json:"bandwidthGb"`
	MedianLatency Num         `json:"medianLatency"`
	LatencySpark  SparkSeries `json:"latencySpark"`
	SuccessSpark  SparkSeries `json:"successSpark"`
	// SuccessScoreSpark is SuccessSpark on the 1-10 scale (marketScoreSpark),
	// with its labels; SuccessScoreDelta the history's change on that scale.
	SuccessScoreSpark ScoreSeries `json:"successScoreSpark"`
	SuccessScoreDelta Num         `json:"successScoreDelta"`
	Fastest           *HomePick   `json:"fastest"`
	MostReliable      *HomePick   `json:"mostReliable"`
	Cheapest          *HomePick   `json:"cheapest"`
	Dearest           *HomePick   `json:"dearest"`
	Spread            HomeSpread  `json:"spread"`
	// SpreadAll is insights.valueAll's spread (homeSpreadAll).
	SpreadAll    *HomeSpread `json:"spreadAll"`
	Authenticity Num         `json:"authenticity"`
	Countries    Num         `json:"countries"`
	Incidents    Num         `json:"incidents"`
	MisSold      Num         `json:"misSold"`
	Duplication  Num         `json:"duplication"`
	// LikelyResellers counts the residential networks with a likely upstream.
	LikelyResellers Num `json:"likelyResellers"`
}

// HomeSeries is a network's sparkline: its values and the index of their
// labels in HomeAnswer.Labels.
type HomeSeries struct {
	Values []Num `json:"values"`
	Labels int   `json:"labels"`
}

// HomeNetwork is one network's panel: its ranking row's figures for the
// range, plus its curve and sparks.
type HomeNetwork struct {
	Slug               string                     `json:"slug"`
	Name               string                     `json:"name"`
	Kind               string                     `json:"kind"`
	Standing           Standing                   `json:"standing"`
	Score              Num                        `json:"score"`
	Grade              model.Str                  `json:"grade"`
	Breakdown          model.Breakdown            `json:"breakdown"`
	Tagline            model.Str                  `json:"tagline"`
	Schedule           model.Str                  `json:"schedule"`
	Requests           Num                        `json:"requests"`
	BandwidthGb        Num                        `json:"bandwidthGb"`
	ObservedDays       Num                        `json:"observedDays"`
	SuccessRate        Num                        `json:"successRate"`
	SuccessDeltaPts    Num                        `json:"successDeltaPts"`
	LatencyP50         Num                        `json:"latencyP50"`
	LatencyP90         Num                        `json:"latencyP90"`
	LatencyP99         Num                        `json:"latencyP99"`
	LightLatencyP50    Num                        `json:"lightLatencyP50"`
	LatencyDeltaPct    Num                        `json:"latencyDeltaPct"`
	LatencyNote        *string                    `json:"latencyNote"`
	UniqueIps          Num                        `json:"uniqueIps"`
	Countries          Num                        `json:"countries"`
	Cities             Num                        `json:"cities"`
	ListRate           Num                        `json:"listRate"`
	ValueRank          *int                       `json:"valueRank"`
	Claimed            Num                        `json:"claimed"`
	ClaimShare         Num                        `json:"claimShare"`
	Authenticity       Num                        `json:"authenticity"`
	DatacenterAsnShare Num                        `json:"datacenterAsnShare"`
	Incidents          Num                        `json:"incidents"`
	BrowserOverall     map[string]RowBrowserScore `json:"browserOverall"`
	SuccessScore       *model.SuccessScore        `json:"successScore"`
	Curve              HomeSeries                 `json:"curve"`
	LatencySpark       HomeSeries                 `json:"latencySpark"`
	SuccessSpark       HomeSeries                 `json:"successSpark"`
	// SuccessScoreSpark is each SuccessSpark point's 1-10 score among the
	// home's networks of the kind at that point (homePointScores).
	SuccessScoreSpark HomeScoreSeries `json:"successScoreSpark"`
	NAList
}

func buildHome(c *Context) (map[string]any, error) {
	if c.Dataset == nil || c.Pages == nil {
		return nil, errors.New("no dataset or pages")
	}
	fleet := []*entry{}
	for _, e := range c.Entries {
		if e.Snap[e.P.PrimaryKind] != nil {
			fleet = append(fleet, e)
		}
	}
	views := measuredViews(fleet, c.Stats, c.Dataset.MeasuredTotals)
	out, byslug := map[string]any{}, entriesBySlug(c.Entries)
	for i, r := range Ranges {
		rows := c.Pages.Rankings[r.ID]
		if rows == nil {
			return nil, errors.New("no rankings for " + r.ID)
		}
		ans := &HomeAnswer{GeneratedAt: c.GeneratedAt, Range: r.ID, Networks: []HomeNetwork{}, Labels: [][]string{}, ScoreDefinitions: scoreDefinitions,
			Tape: homeTape(c.Dataset.Tape), Profiles: c.Dataset.Profiles}
		ans.Market = homeMarket(c, fleet, &views[i], r.ID)
		ans.Insights = homeInsights(c, rows.Networks, r.ID)
		ans.Market.SpreadAll = homeSpreadAll(ans.Insights.ValueAll)
		scores := homePointScores(rows.Networks, byslug, c.Stats, r.ID)
		for _, row := range rows.Networks {
			n := ans.network(row, byslug[row.Slug], c.Stats[row.Slug][r.ID], r)
			n.SuccessScoreSpark = scoreSpark(scores[row.Slug], homeNetworkSpark, n.SuccessSpark.Labels)
			ans.Networks = append(ans.Networks, n)
		}
		if err := ans.listNA(rows.Networks); err != nil {
			return nil, err
		}
		out[homePath(r.ID)] = ans
	}
	return out, nil
}

// homeTape is the newest store.TapeHeadRows rows of each tape tab among rows
// (the dataset's tape, newest first), merged newest first: every tab of the
// home's tape has rows when the tab has events, and a tab without stays empty.
func homeTape(rows []model.TapeEvent) []model.TapeEvent {
	out, taken := []model.TapeEvent{}, map[string]int{}
	for _, r := range rows {
		if slices.Contains(store.TapeTabs, r.Kind) && taken[r.Kind] < store.TapeHeadRows {
			out, taken[r.Kind] = append(out, r), taken[r.Kind]+1
		}
	}
	return out
}

func entriesBySlug(entries []*entry) map[string]*entry {
	out := make(map[string]*entry, len(entries))
	for _, e := range entries {
		out[e.P.Slug] = e
	}
	return out
}

// homeMarket is the market panel of one range from the fleet (the networks
// with a published headline group), its measured view v and the
// all-networks answer (requests, distinct IPs and countries: each counted
// once over every product group).
func homeMarket(c *Context, fleet []*entry, v *FleetView, rangeID string) HomeMarket {
	m := HomeMarket{Requests: nan, UniqueIps: nan, Countries: nan, Targets: targetCounts(c.Dataset.Targets).Measured, Networks: len(fleet),
		Curve: v.HeadlineSpark, MedianSuccess: v.Fleet.MedianSuccessRate, BandwidthGb: v.Fleet.BandwidthGb,
		MedianLatency: v.Fleet.MedianLatency, LatencySpark: v.LatencySpark, SuccessSpark: v.SuccessSpark,
		ObservedDays: fleetObservedDays(v.Fleet.Success, rangeID), Spread: HomeSpread{Value: v.Bills.SpreadX}}
	m.SuccessScore = marketScore(m.MedianSuccess)
	m.SuccessScoreSpark, m.SuccessScoreDelta = marketScoreSpark(m.SuccessSpark), marketScoreDelta(v.Fleet.Success)
	if all := c.Pages.Network[AllNetworks+"/"+AllNetworks]; all != nil && all.Ranges[rangeID] != nil {
		t := all.Ranges[rangeID]
		m.Requests, m.UniqueIps, m.Countries = t.Requests, t.UniqueIps, Num(t.ExitCountries)
	}
	if s := v.Fastest.Stats; s != nil {
		m.Fastest = &HomePick{Slug: v.Fastest.Provider.Slug, Name: v.Fastest.Provider.Name, Value: s.LatencyP50}
	}
	if s := v.MostReliable.Stats; s != nil {
		m.MostReliable = &HomePick{Slug: v.MostReliable.Provider.Slug, Name: v.MostReliable.Provider.Name, Value: s.SuccessRate}
	}
	m.Cheapest, m.Dearest = costPick(v.Cheapest), costPick(v.Dearest)
	if b := v.Bills; b.Best != nil {
		m.Spread.Best, m.Spread.Worst = &HomeRef{b.Best.Provider.Slug, b.Best.Provider.Name}, &HomeRef{b.Worst.Provider.Slug, b.Worst.Provider.Name}
	}
	m.Authenticity, m.MisSold, m.Incidents = poolShares(fleet, c.Stats, rangeID)
	market := OverlapFor(c, overlapDefaultKind, rangeID).Market
	m.Duplication, m.LikelyResellers = market.DuplicatedShare, Num(len(market.LikelyResellers))
	return m
}

func costPick(p CostPick) *HomePick {
	if p.Provider == nil || !p.Cost.OK() {
		return nil
	}
	return &HomePick{Slug: p.Provider.Slug, Name: p.Provider.Name, Value: p.Cost}
}

// fleetObservedDays is observedDays for the fleet: the oldest point of its
// success history (ages in minutes, oldest first), in days rounded up; 0
// below a day; NaN when it covers the range or there is no point.
func fleetObservedDays(history []model.SeriesPoint, rangeID string) Num {
	if len(history) == 0 || !history[0].T.OK() {
		return nan
	}
	days := history[0].T.F() / dayMinutes
	switch {
	case days >= float64(rangeMeta(rangeID).Days):
		return nan
	case days < 1:
		return 0
	}
	return Num(math.Ceil(days))
}

// poolShares is, over the fleet's range windows of the networks sold as
// non-datacenter, the share of exit IPs in the requested country
// (pool.geo_accuracy) and on hosting ASNs (pool.datacenter_asn_share), each
// weighted by the window's exit IPs; and the fleet's incidents.
func poolShares(fleet []*entry, stats map[string]map[string]*WindowStats, rangeID string) (authentic, misSold, incidents Num) {
	var ips, geo, dcIps, dc float64
	incidents = nan
	for _, e := range fleet {
		incidents = addNum(incidents, stats[e.P.Slug][rangeID].IncidentCount)
		sp := e.Snap[e.P.PrimaryKind]
		if sp == nil || e.P.PrimaryKind == "datacenter" {
			continue
		}
		p := sp.Window(rangeID).Pool
		if n := p.UniqueIps; n.OK() && n > 0 && p.GeoAccuracy.OK() {
			ips, geo = ips+n.F(), geo+n.F()*p.GeoAccuracy.F()
		}
		if n := p.UniqueIps; n.OK() && n > 0 && p.DatacenterAsnShare.OK() {
			dcIps, dc = dcIps+n.F(), dc+n.F()*p.DatacenterAsnShare.F()
		}
	}
	return ratio(Num(geo), Num(ips)), ratio(Num(dc), Num(dcIps)), incidents
}

// network is a ranking row's panel, with e's curve and sparks (e is nil for
// a row without an entry, which has none).
func (a *HomeAnswer) network(row RankingRow, e *entry, s *WindowStats, r RangeMeta) HomeNetwork {
	n := HomeNetwork{Slug: row.Slug, Name: row.Name, Kind: row.Kind, Standing: row.Standing, Score: row.Score, Grade: row.Grade,
		Breakdown: row.Breakdown, Tagline: row.Tagline, Schedule: schedule(row, e), Requests: row.Requests, BandwidthGb: row.BandwidthGb,
		ObservedDays: row.ObservedDays, SuccessRate: row.SuccessRate, SuccessDeltaPts: row.SuccessDelta,
		LatencyP50: row.LatencyP50, LatencyP90: row.LatencyP90, LatencyP99: row.LatencyP99, LatencyDeltaPct: row.LatencyDelta,
		LatencyNote: row.LatencyNote, UniqueIps: row.UniqueIps, Countries: row.Countries, Cities: row.Cities,
		ListRate: row.ListRate, ValueRank: row.ValueRank, Claimed: nan, ClaimShare: nan, Authenticity: row.Authenticity,
		DatacenterAsnShare: row.DatacenterAsnShare, Incidents: nan, LightLatencyP50: nan, BrowserOverall: row.BrowserOverall,
		SuccessScore: row.SuccessScore}
	var success, latency []model.SeriesPoint
	if s != nil {
		n.Incidents, n.LightLatencyP50 = s.IncidentCount, s.LightLatencyP50
	}
	if e != nil && s != nil {
		n.Claimed = e.P.Pool.ClaimedPoolSize
		n.ClaimShare = ratio(row.UniqueIps, n.Claimed)
		successHistory, latencyHistory := entryHistory(e, s, r.ID)
		success = successHistory
		for _, p := range latencyHistory {
			latency = append(latency, model.SeriesPoint{T: p.T, V: p.P50})
		}
	}
	curve := len(success)
	if r.Days == 1 {
		curve = homeCurve24h
	}
	n.Curve, n.SuccessSpark = a.series(sparkSeries(success, curve)), a.series(sparkSeries(success, homeNetworkSpark))
	n.LatencySpark = a.series(sparkSeries(latency, homeNetworkSpark))
	return n
}

// series keeps s's labels once in the answer's Labels.
func (a *HomeAnswer) series(s SparkSeries) HomeSeries {
	i := slices.IndexFunc(a.Labels, func(l []string) bool { return slices.Equal(l, s.Labels) })
	if i < 0 {
		i = len(a.Labels)
		a.Labels = append(a.Labels, s.Labels)
	}
	return HomeSeries{Values: s.Values, Labels: i}
}

// schedule is what a measured network's row is measured on (measuredSchedule
// of its product), never read from its tagline, which the catalog may set;
// null without a measured product.
func schedule(row RankingRow, e *entry) model.Str {
	if row.Source != model.SourceMeasured || e == nil || e.Snap[row.Kind] == nil {
		return nil
	}
	return model.S(measuredSchedule(e.Snap[row.Kind]))
}
