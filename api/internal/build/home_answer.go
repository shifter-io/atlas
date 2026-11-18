package build

import (
	"errors"
	"math"
	"net/http"
	"slices"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/store"
)

// /v1/home?range= (api/answers/home.md): the Atlas home's live
// dashboard in one body per range: the market panel, the insight cards,
// every network's panel with its score curve and sparks, and each tab's newest tape rows.

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

// Decimals of the home chart series (HOME-SIZE-1, #905): they are drawn, not
// read as figures, so they are rounded for room under MaxAnswerBytes. Rates
// keep 0.01-point resolution, latencies whole ms, score sparks 2 decimals;
// every headline figure stays unrounded.
const (
	homeRateDecimals    = 4
	homeLatencyDecimals = 0
	homeScoreDecimals   = 2
)

// HomeAnswer is one range of /v1/home.
type HomeAnswer struct {
	GeneratedAt string        `json:"generatedAt"`
	Range       string        `json:"range"`
	Market      HomeMarket    `json:"market"`
	Insights    HomeInsights  `json:"insights"`
	Networks    []HomeNetwork `json:"networks"`
	// Times is every distinct times list of the networks' series, once: a
	// series' timesIndex is its index here, each time in minutes before the
	// build's as-of time (SPARK-TIMES-1).
	Times    [][]Num                `json:"times"`
	Tape     []model.TapeEvent      `json:"tape"`
	Profiles []model.RankingProfile `json:"profiles"`
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
	Requests     Num `json:"requests"`
	ObservedDays Num `json:"observedDays"`
	Networks     int `json:"networks"`
	Targets      int `json:"targets"`
	// SuccessScoreCurve is the fleet's success history pooled by requests
	// per UTC hour (24h) or day (7d, 30d; fleetCurve, CURVE-HOURLY-DAILY-1)
	// on the 1-10 scale (scoreCurve); the rates stay internal
	// (SUCCESS-CURVE-1, #1072).
	SuccessScoreCurve SparkSeries `json:"successScoreCurve"`
	MedianSuccess     Num         `json:"-"` // internal (SUCCESS-RATE-REMOVE-1, #900): successScore is served
	// SuccessScore is MedianSuccess on the 1-10 scale (marketScore).
	SuccessScore MarketScore `json:"successScore"`
	UniqueIps    Num         `json:"uniqueIps"`
	// UniqueIpsCurve is the fleet's distinct exit IPs at each UTC hour over
	// the trailing 24 hours (24h) or per UTC day (7d, 30d), each IP once
	// across networks, an unfinished last day flagged (uniqueIpsCurve).
	UniqueIpsCurve UniqueIpsSeries `json:"uniqueIpsCurve"`
	BandwidthGb    Num             `json:"bandwidthGb"`
	MedianLatency  Num             `json:"medianLatency"`
	LatencySpark   SparkSeries     `json:"latencySpark"`
	SuccessSpark   SparkSeries     `json:"-"` // internal (#900)
	// SuccessScoreSpark is SuccessSpark on the 1-10 scale (marketScoreSpark),
	// with its times; SuccessScoreDelta the history's change on that scale.
	SuccessScoreSpark MarketScoreSeries `json:"successScoreSpark"`
	SuccessScoreDelta Num               `json:"successScoreDelta"`
	Fastest           *HomePick         `json:"fastest"`
	MostReliable      *HomeReliable     `json:"mostReliable"`
	Cheapest          *HomePick         `json:"cheapest"`
	Dearest           *HomePick         `json:"dearest"`
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
// times in HomeAnswer.Times (SERIES-TIMES-INDEX-1).
type HomeSeries struct {
	Values     []Num `json:"values"`
	TimesIndex int   `json:"timesIndex"`
}

// HomeNetwork is one network's panel: its ranking row's figures for the
// range, plus its score curve and sparks.
type HomeNetwork struct {
	Slug                string                     `json:"slug"`
	Name                string                     `json:"name"`
	Kind                string                     `json:"kind"`
	Standing            Standing                   `json:"standing"`
	Score               Num                        `json:"score"`
	Grade               model.Str                  `json:"grade"`
	Breakdown           model.Breakdown            `json:"breakdown"`
	Requests            Num                        `json:"requests"`
	BandwidthGb         Num                        `json:"bandwidthGb"`
	ObservedDays        Num                        `json:"observedDays"`
	SuccessRate         Num                        `json:"-"` // internal (SUCCESS-RATE-REMOVE-1, #900): successScore is served
	SuccessDeltaPts     Num                        `json:"-"` // internal (#900): successScoreDelta is served
	LatencyP50          Num                        `json:"latencyP50"`
	LatencyP90          Num                        `json:"latencyP90"`
	LatencyP99          Num                        `json:"latencyP99"`
	LightLatencyP50     Num                        `json:"lightLatencyP50"`
	UnderLoadLatencyP50 Num                        `json:"underLoadLatencyP50"` // display only (WindowFigures)
	LatencyDeltaPct     Num                        `json:"latencyDeltaPct"`
	LatencyDeltaBasis   model.Str                  `json:"latencyDeltaBasis"`
	LatencyDeltaAsOf    model.Str                  `json:"latencyDeltaAsOf"`
	UniqueIps           Num                        `json:"uniqueIps"`
	Countries           Num                        `json:"countries"`
	Cities              Num                        `json:"cities"`
	ListRate            Num                        `json:"listRate"`
	Claimed             Num                        `json:"claimed"`
	ClaimShare          Num                        `json:"claimShare"`
	Authenticity        Num                        `json:"authenticity"`
	DatacenterAsnShare  Num                        `json:"datacenterAsnShare"`
	DatacenterIps       Num                        `json:"datacenterIps"`
	Incidents           Num                        `json:"incidents"`
	BrowserOverall      map[string]RowBrowserScore `json:"browserOverall"`
	// ValueRankAllTargets is the ranking row's (VALUE-ALL-RANK-1).
	ValueRankAllTargets *int `json:"valueRankAllTargets"`
	// SuccessScore is the ranking row's without its targets and weights
	// (the network page and /v1/targets serve them): the answer's room.
	SuccessScore      *model.MetricScore `json:"successScore"`
	SuccessScoreDelta Num                `json:"successScoreDelta"`
	// SuccessScoreCurve is the success history per UTC hour or day of the
	// range's curve axis, weighted by requests (networkCurve,
	// CURVE-HOURLY-DAILY-1), each point on the 1-10 scale (scoreCurve;
	// SUCCESS-CURVE-1, #1072).
	SuccessScoreCurve HomeSeries `json:"successScoreCurve"`
	LatencySpark      HomeSeries `json:"latencySpark"`
	SuccessSpark      HomeSeries `json:"-"` // internal (#900): successScoreSpark is served
	// SuccessScoreSpark is each SuccessSpark point's 1-10 score among the
	// window's Success x/10 rule over the daily tests of the trailing window ending at that hour or day (axisScores).
	SuccessScoreSpark HomeScoreSeries `json:"successScoreSpark"`
	// AtlasScoreSpark is the ranking row's Atlas Score trend, its times in
	// the answer's Times (ATLAS-SCORE-SPARK-1).
	AtlasScoreSpark HomeScoreSeries `json:"atlasScoreSpark"`
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
		ans := &HomeAnswer{GeneratedAt: c.GeneratedAt, Range: r.ID, Networks: []HomeNetwork{}, Times: [][]Num{},
			Tape: tabHeads(c.Dataset.Tape), Profiles: c.Dataset.Profiles}
		ans.Market = homeMarket(c, fleet, &views[i], r.ID)
		ans.Insights = homeInsights(c, rows.Networks, r.ID)
		ans.Market.SpreadAll = homeSpreadAll(ans.Insights.ValueAll)
		asOf, ok := curveAsOf(fleet, c.Dataset, r.ID)
		axis := newCurveAxis(asOf, ok, r)
		ans.marketSeries(fleet, c.Stats, axis, r.ID)
		hists := homeHistories(rows.Networks, byslug, c.Stats, r.ID, asOf)
		for j, row := range rows.Networks {
			n := ans.network(row, byslug[row.Slug], c.Stats[row.Slug][r.ID], hists[j], axis)
			n.SuccessScoreSpark = HomeScoreSeries{ScoreSeries: scoreSeries(axisScores(hists[j], axis)), TimesIndex: n.SuccessScoreCurve.TimesIndex}
			ans.Networks = append(ans.Networks, n)
		}
		if err := ans.listNA(rows.Networks); err != nil {
			return nil, err
		}
		out[homePath(r.ID)] = ans
	}
	return out, nil
}

// tabHeads is the newest store.TapeHeadRows rows of each tape tab among rows
// (the dataset's tape, newest first), merged newest first: every tab of the
// home's tape has rows when the tab has events, and a tab without stays empty.
func tabHeads(rows []model.TapeEvent) []model.TapeEvent {
	return kindHeads(rows, func(kind string) bool { return slices.Contains(store.TapeTabs, kind) })
}

// kindHeads is the newest store.TapeHeadRows rows of each kind among rows
// (newest first) that keep admits, merged newest first. A network answer's
// tape is every kind's (NETWORK-TAPE-QUOTA-1): its tabs' and an editorial
// row's own kind.
func kindHeads(rows []model.TapeEvent, keep func(kind string) bool) []model.TapeEvent {
	out, taken := []model.TapeEvent{}, map[string]int{}
	for _, r := range rows {
		if keep(r.Kind) && taken[r.Kind] < store.TapeHeadRows {
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
		MedianSuccess: v.Fleet.MedianSuccessRate, BandwidthGb: v.Fleet.BandwidthGb,
		MedianLatency: v.Fleet.MedianLatency, ObservedDays: fleetObservedDays(v.Fleet.Success, rangeID),
		UniqueIpsCurve: uniqueIpsCurve(c.Snapshot, rangeID)}
	m.SuccessScore = marketScore(m.MedianSuccess)
	m.SuccessScoreDelta = marketScoreDelta(v.Fleet.Success)
	if all := c.Pages.Network[AllNetworks+"/"+AllNetworks]; all != nil && all.Ranges[rangeID] != nil {
		t := all.Ranges[rangeID]
		m.Requests, m.UniqueIps, m.Countries = t.Requests, t.UniqueIps, Num(t.ExitCountries)
	}
	if s := v.Fastest.Stats; s != nil {
		m.Fastest = &HomePick{Slug: v.Fastest.Provider.Slug, Name: v.Fastest.Provider.Name, Value: s.LatencyP50}
	}
	m.MostReliable = mostReliable(fleet, c.Stats, rangeID, v.MostReliable)
	m.Cheapest, m.Dearest = costPick(v.Cheapest), costPick(v.Dearest)
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

// network is a ranking row's panel, with its score curve on the range's axis and
// its sparks from h (empty for a row without an entry).
func (a *HomeAnswer) network(row RankingRow, e *entry, s *WindowStats, h homeHistory, axis curveAxis) HomeNetwork {
	n := HomeNetwork{Slug: row.Slug, Name: row.Name, Kind: row.Kind, Standing: row.Standing, Score: row.Score, Grade: row.Grade,
		Breakdown: row.Breakdown, Requests: row.Requests, BandwidthGb: row.BandwidthGb,
		ObservedDays: row.ObservedDays, SuccessRate: row.SuccessRate, SuccessDeltaPts: row.SuccessDelta,
		LatencyP50: row.LatencyP50, LatencyP90: row.LatencyP90, LatencyP99: row.LatencyP99, LatencyDeltaPct: row.LatencyDelta,
		LatencyDeltaBasis: row.LatencyBasis, LatencyDeltaAsOf: row.LatencyAsOf,
		UniqueIps: row.UniqueIps, Countries: row.Countries, Cities: row.Cities,
		ListRate: row.ListRate, ValueRankAllTargets: row.ValueRankAllTargets, Claimed: nan, ClaimShare: nan, Authenticity: row.Authenticity,
		DatacenterAsnShare: row.DatacenterAsnShare, DatacenterIps: row.DatacenterIps, Incidents: nan, LightLatencyP50: nan, UnderLoadLatencyP50: row.UnderLoadLatencyP50, BrowserOverall: row.BrowserOverall,
		SuccessScore: metricOf(row.SuccessScore), SuccessScoreDelta: row.SuccessScoreDelta}
	if s != nil {
		n.Incidents, n.LightLatencyP50 = s.IncidentCount, s.LightLatencyP50
	}
	if e != nil && s != nil {
		n.Claimed = e.P.Pool.ClaimedPoolSize
		n.ClaimShare = ratio(row.UniqueIps, n.Claimed)
	}
	rates := networkCurve(h.curve, axis)
	n.SuccessScoreCurve = a.series(scoreCurve(rates))
	n.SuccessSpark = a.series(roundedSeries(rates, homeRateDecimals))
	n.LatencySpark = a.series(roundedSeries(latencySpark(h.latency, axis), homeLatencyDecimals))
	n.AtlasScoreSpark = a.atlasSpark(row)
	return n
}

// series keeps s's times once in the answer's Times: one entry per distinct
// times list.
func (a *HomeAnswer) series(s SparkSeries) HomeSeries {
	i := slices.IndexFunc(a.Times, func(t []Num) bool { return slices.Equal(t, s.Times) })
	if i < 0 {
		i, a.Times = len(a.Times), append(a.Times, s.Times)
	}
	return HomeSeries{Values: s.Values, TimesIndex: i}
}

// metricOf is s without its targets and weights; nil (null) when s is.
func metricOf(s *model.SuccessScore) *model.MetricScore {
	if s == nil {
		return nil
	}
	return &s.MetricScore
}
