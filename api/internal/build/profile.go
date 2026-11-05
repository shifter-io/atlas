package build

import (
	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
	"github.com/Zeus-Development/proxy-index-website/api/internal/snap"
)

// NetworkProfile is what a network page prints about the network in one
// category: its presentation, score and that category's measured blocks.
// The long lists are other answers: places (geo, ASNs) are paged by
// /v1/networks/{slug}/places, histories are /v1/networks/{slug}/chart, and
// the raw snapshot product is not repeated.
type NetworkProfile struct {
	Source          string                  `json:"source"`
	Slug            string                  `json:"slug"`
	Name            string                  `json:"name"`
	Tagline         model.Str               `json:"tagline"`
	Kinds           []string                `json:"kinds"`
	PrimaryKind     string                  `json:"primaryKind"`
	HQ              model.Str               `json:"hq"`
	Founded         Num                     `json:"founded"`
	Claim           model.Str               `json:"claim"`
	Verdict         model.Str               `json:"verdict"`
	Strengths       []string                `json:"strengths"`
	Weaknesses      []string                `json:"weaknesses"`
	BestFor         []string                `json:"bestFor"`
	Website         model.Str               `json:"website"`
	Products        []model.ProviderProduct `json:"products"`
	MeasuredProduct string                  `json:"measuredProduct"`
	Score           model.ScoreView         `json:"score"`
	Reviews         model.ReviewSummary     `json:"reviews"`
	Framework       *model.FrameworkLink    `json:"framework"`
	// The category's measured blocks (Provider.productMeasurements.<kind>).
	Pool        model.PoolStats         `json:"pool"`
	Performance model.PerformanceStats  `json:"performance"`
	Pricing     model.Pricing           `json:"pricing"`
	Trust       model.TrustSignals      `json:"trust"`
	Targets     []model.TargetBenchmark `json:"targets"`
	IPSamples   []model.IPSample        `json:"ipSamples"`
	Incidents   []model.Incident        `json:"incidents"`
	Overlap     model.OverlapSummary    `json:"overlap"`
	// TileRanks is the category's precomputed ranks by range.
	TileRanks map[string]model.TileRanks `json:"tileRanks"`
	NA        []string                   `json:"na"`
}

// RangeStats is a range's WindowStats without its histories, which are the
// chart answer's (the five shadowing fields are always nil, so omitted).
type RangeStats struct {
	*WindowStats
	Success      *struct{} `json:"success,omitempty"`
	Availability *struct{} `json:"availability,omitempty"`
	Latency      *struct{} `json:"latency,omitempty"`
	Pool         *struct{} `json:"pool,omitempty"`
	Bands        *struct{} `json:"bands,omitempty"`
}

// ChartAnswer is /v1/networks/{slug}/chart?kind=&range=: the histories a
// network page draws for one category and range (window.ts WindowStats'
// series). Times are minutes before the build's as-of time.
type ChartAnswer struct {
	GeneratedAt  string               `json:"generatedAt"`
	Network      string               `json:"network"`
	Kind         string               `json:"kind"`
	Range        string               `json:"range"`
	Success      []model.SeriesPoint  `json:"success"`
	Availability []model.SeriesPoint  `json:"availability"`
	Latency      []model.LatencyPoint `json:"latency"`
	Pool         []model.SeriesPoint  `json:"pool"`
	Bands        []IncidentBand       `json:"bands"`
}

func networkProfile(p *model.Provider, kind string) *NetworkProfile {
	m := p.Measured
	if pm := p.ProductMeasurements.Get(kind); pm != nil {
		m = pm.Measured
	}
	return &NetworkProfile{Source: p.Source, Slug: p.Slug, Name: p.Name, Tagline: p.Tagline, Kinds: p.Kinds, PrimaryKind: p.PrimaryKind,
		HQ: p.HQ, Founded: p.Founded, Claim: p.Claim, Verdict: p.Verdict, Strengths: p.Strengths, Weaknesses: p.Weaknesses,
		BestFor: p.BestFor, Website: p.Website, Products: p.Products, MeasuredProduct: p.MeasuredProduct, Score: p.Score,
		Reviews: p.Reviews, Framework: p.Framework, Pool: m.Pool, Performance: m.Performance, Pricing: m.Pricing, Trust: m.Trust,
		Targets: m.Targets, IPSamples: m.IPSamples, Incidents: m.Incidents, Overlap: m.Overlap, TileRanks: p.TileRanks[kind], NA: p.NA}
}

// addDetails completes every network answer with its profile, its window
// statistics without their histories, and its records, and adds a chart
// answer per range holding those histories.
func (pg *Pages) addDetails(generatedAt string, details map[string]*ProviderDetail) {
	for key, ans := range pg.Network {
		d := details[ans.Network.Slug]
		if d == nil {
			continue
		}
		ans.Profile = networkProfile(d.Provider, ans.Kind)
		ans.Reviews, ans.CommunityRuns = newest(d.Reviews), newest(d.CommunityRuns)
		ans.Changelog, ans.Tape = newest(d.Changelog), newest(d.Tape)
		ans.Records = RecordCounts{Reviews: len(d.Reviews), CommunityRuns: len(d.CommunityRuns), Changelog: len(d.Changelog), Tape: len(d.Tape)}
		for rangeID, ws := range d.Windows[ans.Kind] {
			pg.Charts[key+"/"+rangeID] = &ChartAnswer{GeneratedAt: generatedAt, Network: ans.Network.Slug, Kind: ans.Kind, Range: rangeID,
				Success: ws.Success, Availability: ws.Availability, Latency: ws.Latency, Pool: ws.Pool, Bands: ws.Bands}
			ans.Stats[rangeID] = &RangeStats{WindowStats: ws}
		}
	}
}

// RecordCap is how many of a network's newest reviews, community runs,
// changelog and tape rows its answer carries (each list is newest first), so
// the answer stays bounded however long its history grows; RecordCounts gives
// the full lengths, and the whole history stays in the dataset file.
const RecordCap = 20

// RecordCounts is the full length of each record list of a network.
type RecordCounts struct {
	Reviews       int `json:"reviews"`
	CommunityRuns int `json:"communityRuns"`
	Changelog     int `json:"changelog"`
	Tape          int `json:"tape"`
}

func newest[T any](rows []T) []T {
	return rows[:min(len(rows), RecordCap)]
}

// allCharts is the all-networks answer's histories: the every-group fleet
// series of each range (success rate, light-load latency and distinct IPs per
// bucket); empty without that fleet entry. Availability and outage bands are
// per product, so they stay empty.
func (pg *Pages) allCharts(generatedAt string, s *snap.Snapshot) {
	for _, r := range Ranges {
		c := &ChartAnswer{GeneratedAt: generatedAt, Network: AllNetworks, Kind: AllNetworks, Range: r.ID, Success: []model.SeriesPoint{},
			Availability: []model.SeriesPoint{}, Latency: []model.LatencyPoint{}, Pool: []model.SeriesPoint{}, Bands: []IncidentBand{}}
		if f := fleetAll(s, r.ID); f != nil {
			ref := snap.Ms(s.AsOf)
			for _, row := range f.Series {
				if !row.Completed.OK() || row.Completed <= 0 {
					continue
				}
				t := minutesAgo(ref, row.At)
				c.Success = append(c.Success, model.SeriesPoint{T: t, V: row.SuccessRate})
				c.Latency = append(c.Latency, model.LatencyPoint{T: t, P50: row.LatencyMs.P50, P90: row.LatencyMs.P90, P99: row.LatencyMs.P99})
				c.Pool = append(c.Pool, model.SeriesPoint{T: t, V: row.UniqueIps})
			}
		}
		pg.Charts[AllNetworks+"/"+AllNetworks+"/"+r.ID] = c
	}
}
