package build

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// DatasetSchemaVersion is the Dataset contract version this API implements
// (src/lib/types.ts).
const DatasetSchemaVersion = "0.6.0"

// MeasuredTapeRows caps how many measured events the dataset's tape carries.
const MeasuredTapeRows = 200

// Result is every response body of one build, as Go values.
type Result struct {
	Fingerprint store.Fingerprint
	BuiltAt     time.Time
	Dataset     *model.Dataset
	// Details are not served whole (/v1/providers is gone): they feed the
	// network answers and charts.
	Details  map[string]*ProviderDetail
	Search   *SearchIndex
	Coverage *Coverage
	// Pages is the small per-page answers (pages.go); it replaces serving the
	// published snapshot payload whole.
	Pages  *Pages
	Events EventWindow
	// SiteGaps is store.Inputs.SiteGaps: the site tables read as empty
	// because the database lacks them (none once `siteapi migrate` ran).
	SiteGaps []string
}

// JSISO is Date.prototype.toISOString.
func JSISO(t time.Time) string {
	return t.UTC().Format("2006-01-02T15:04:05.000Z")
}

// prepareMedian is scripts/prepare-design-demo.mjs median.
func prepareMedian(v []float64) float64 {
	if len(v) == 0 {
		return math.NaN()
	}
	s := append([]float64{}, v...)
	jsSortNumbers(s)
	return (s[(len(s)-1)/2] + s[len(s)/2]) / 2
}

// fleetTotals ports the totals block of scripts/prepare-design-demo.mjs over
// the fleet basis; review and run counts cover every record.
func fleetTotals(all, fleet []*entry, reviews []model.UserReview, runs []model.CommunityRun) model.FleetTotals {
	sum := func(f func(p *model.Provider) float64) float64 {
		s := 0.0
		for _, e := range fleet {
			s += f(e.P)
		}
		return s
	}
	var t model.FleetTotals
	t.Providers = Num(len(all))
	t.Requests24h = Num(sum(func(p *model.Provider) float64 { return p.Performance.Requests24h.F() }))
	ips := sum(func(p *model.Provider) float64 { return p.Pool.UniqueIps24h.F() })
	t.UniqueIps24h = Num(ips)
	s := scanFleet(fleet)
	distinct := math.Max(1, ips-s.shared)
	if len(fleet) == 0 {
		distinct = math.NaN()
	}
	t.Countries = Num(len(s.countries))
	t.MedianLatency = Num(prepareMedian(s.lat))
	t.MedianSuccessRate = Num(prepareMedian(s.sr))
	t.Incidents24h = Num(s.incidents)
	t.BandwidthGb24h = Num(sum(func(p *model.Provider) float64 { return p.Performance.BandwidthMb24h.F() }) / 1000)
	t.MisclassifiedShare = Num(sum(func(p *model.Provider) float64 {
		return float64(p.Pool.UniqueIps24h.F() * p.Trust.DatacenterAsnShare.F())
	}) / ips)
	t.DistinctIpsAcrossFleet = Num(distinct)
	t.FleetDuplicationShare = Num(1 - distinct/ips)
	t.LikelyResellers = Num(s.resellers)
	if len(fleet) == 0 {
		t.Requests24h, t.UniqueIps24h, t.Countries, t.Incidents24h, t.LikelyResellers, t.BandwidthGb24h = nan, nan, nan, nan, nan, nan
	}
	recordCounts(&t, reviews, runs)
	return t
}

// fleetScan is what fleetTotals gathers in one pass over the fleet.
type fleetScan struct {
	countries map[string]bool
	lat, sr   []float64
	// shared is the shared IPs of every overlapping pair, each pair once.
	shared               float64
	incidents, resellers int
}

// scanFleet is the per-provider loop of the prepare-design-demo.mjs totals
// block.
func scanFleet(fleet []*entry) fleetScan {
	s := fleetScan{countries: map[string]bool{}, lat: []float64{}, sr: []float64{}}
	pairs := map[string]float64{}
	pairOrder := []string{}
	for _, e := range fleet {
		p := e.P
		for _, g := range p.Geo {
			s.countries[g.CountryCode] = true
		}
		s.lat = append(s.lat, p.Performance.LatencyP50.F())
		s.sr = append(s.sr, p.Performance.SuccessRate.F())
		for _, o := range p.Overlap.Overlaps {
			k := []string{p.Slug, o.Slug}
			sort.Strings(k)
			key := strings.Join(k, "|")
			if _, ok := pairs[key]; !ok {
				pairOrder = append(pairOrder, key)
			}
			pairs[key] = o.SharedIps.F()
		}
		for _, inc := range p.Incidents {
			if inc.StartedAgoSeconds <= 86400 {
				s.incidents++
			}
		}
		if p.Overlap.LikelyUpstream != nil {
			s.resellers++
		}
	}
	for _, k := range pairOrder {
		s.shared += pairs[k]
	}
	return s
}

// recordCounts sets the review and run counts of t.
func recordCounts(t *model.FleetTotals, reviews []model.UserReview, runs []model.CommunityRun) {
	published, verified, accepted := 0, 0, 0
	for _, r := range reviews {
		if r.Verification != "pending" {
			published++
		}
		if r.Verification == "invoice_verified" || r.Verification == "run_verified" {
			verified++
		}
	}
	for _, r := range runs {
		if r.Status == "accepted" {
			accepted++
		}
	}
	t.Reviews, t.VerifiedReviews, t.CommunityRuns = Num(published), Num(verified), Num(accepted)
}

func jsRound2(x float64) Num { return Num(jsnum.Round(x*100) / 100) }

// reviewSummary is generate-demo-data.ts summariseReviews, with an
// unavailable mean (null) instead of 0 when there is nothing to average.
func reviewSummary(slug string, reviews []model.UserReview) model.ReviewSummary {
	s := model.ReviewSummary{Mean: nan, VerifiedMean: nan}
	total, vtotal, n, vn := 0.0, 0.0, 0, 0
	for _, r := range reviews {
		if r.ProviderSlug != slug || r.Verification == "pending" {
			continue
		}
		n++
		total += r.Rating.F()
		if i := int(r.Rating.F()) - 1; i >= 0 && i < 5 {
			s.Distribution[i]++
		}
		if r.Verification == "invoice_verified" || r.Verification == "run_verified" {
			vn++
			vtotal += r.Rating.F()
		}
	}
	s.Count, s.VerifiedCount = Num(n), Num(vn)
	if n > 0 {
		s.Mean = jsRound2(total / float64(n))
	}
	if vn > 0 {
		s.VerifiedMean = jsRound2(vtotal / float64(vn))
	}
	return s
}

func decodeRaw[T any](raw json.RawMessage, what string) ([]T, error) {
	out := []T{}
	if len(raw) == 0 {
		return out, nil
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("%s: %w", what, err)
	}
	if out == nil {
		out = []T{}
	}
	return out, nil
}

// providerForProduct is data.ts providerForProduct.
func providerForProduct(p *model.Provider, kind string) *model.Provider {
	m := p.ProductMeasurements.Get(kind)
	if m == nil {
		return nil
	}
	cp := *p
	cp.PrimaryKind = kind
	cp.MeasuredProduct = m.Product
	cp.Measured = m.Measured
	return &cp
}

// Build assembles every response from one consistent read.
func Build(in *store.Inputs) (*Result, error) {
	return BuildContext(context.Background(), in)
}

// BuildContext is Build that stops with ctx's error once ctx ends (a
// shutdown): it checks between its steps and between the networks of its
// longest, the pages, whose network list reads also take ctx.
func BuildContext(ctx context.Context, in *store.Inputs) (*Result, error) {
	b, err := newBuilder(in)
	if err != nil {
		return nil, err
	}
	b.ctx = ctx
	steps := []func() error{b.catalog, b.providers, b.siteRecords, b.tape, b.measured, b.runScores, b.naManifests, b.recordCoverage, b.datasetNA}
	for _, step := range steps {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if err := step(); err != nil {
			return nil, err
		}
	}
	b.details()
	if err := b.assemble(); err != nil {
		return nil, err
	}
	return b.res, nil
}

// builder is the state one Build shares between its steps, in step order.
type builder struct {
	ctx             context.Context
	in              *store.Inputs
	res             *Result
	s               *snap.Snapshot
	ds              *model.Dataset
	generated       time.Time
	entries         []*entry
	names           map[string]string
	slugOfFramework map[string]string
	stats           map[string]map[string]*WindowStats
	records         map[string]CoverageRecords
}

// newBuilder reads the published snapshot and fills the dataset's header.
func newBuilder(in *store.Inputs) (*builder, error) {
	res := &Result{Fingerprint: in.Fingerprint, BuiltAt: time.Now().UTC(), Details: map[string]*ProviderDetail{}, SiteGaps: in.SiteGaps}
	var s *snap.Snapshot
	if in.Snapshot != nil {
		s = &snap.Snapshot{}
		if err := json.Unmarshal(in.Snapshot.Full(), s); err != nil {
			return nil, fmt.Errorf("published snapshot %d: %w", in.Snapshot.BuildID, err)
		}
		shiftSuccessScores(s)
	}
	ds := &model.Dataset{SchemaVersion: DatasetSchemaVersion, Seed: nan, WindowHours: 24}
	ds.GeneratedAt = JSISO(in.ReadAt)
	if s != nil {
		ds.GeneratedAt = s.AsOf
	}
	generated, _ := snap.ParseTime(ds.GeneratedAt)
	if s != nil {
		ds.RunID = model.S("preview-" + s.SnapshotID)
		ds.CollectorVersion = s.ImplementationID
		ds.Snapshot = model.SnapshotMeta{Available: true, SnapshotID: model.S(s.SnapshotID), SchemaVersion: s.SchemaVersion,
			AsOf: model.S(s.AsOf), GeneratedAt: model.S(s.GeneratedAt), RefreshSeconds: s.RefreshSeconds,
			MethodologyID: model.S(s.Methodology.ID), Readiness: model.S(s.Readiness), ContentHash: model.S(in.Snapshot.ContentHash)}
	} else {
		ds.Snapshot = model.SnapshotMeta{SchemaVersion: nan, RefreshSeconds: nan}
	}
	return &builder{in: in, res: res, s: s, ds: ds, generated: generated}, nil
}

func (b *builder) ageOf(at time.Time) Num {
	return Num(math.Max(0, jsnum.Round(b.generated.Sub(at).Seconds())))
}

func (b *builder) nameOrSlug(slug string) string {
	if n, ok := b.names[slug]; ok {
		return n
	}
	return slug
}

// measured fills the review summaries, the provider list, the totals, the
// window statistics of every provider (headline) and the measured market.
func (b *builder) measured() error {
	ds, entries := b.ds, b.entries
	for _, e := range entries {
		e.P.Reviews = reviewSummary(e.P.Slug, ds.Reviews)
	}
	ds.Providers = make([]*model.Provider, len(entries))
	for i, e := range entries {
		ds.Providers[i] = e.P
	}
	ds.Totals = fleetTotals(entries, entries, ds.Reviews, ds.CommunityRuns)
	ds.MeasuredTotals = fleetTotals(entries, entries, nil, nil)
	b.stats = map[string]map[string]*WindowStats{}
	for _, e := range entries {
		b.stats[e.P.Slug] = map[string]*WindowStats{}
		for _, r := range Ranges {
			b.stats[e.P.Slug][r.ID] = windowStats(e.P, e.Snap[e.P.PrimaryKind], r.ID, cardTargets(ds.Targets))
		}
	}
	var err error
	ds.MeasuredMarket, err = measuredMarket(entries, b.stats, ds.MeasuredTotals, ds.Targets)
	return err
}

// sortByAge orders records newest first (data.ts sorts by agoSeconds ascending), stably.
func sortByAge[T any](rows []T, age func(T) Num) {
	sort.SliceStable(rows, func(i, j int) bool { return age(rows[i])-age(rows[j]) < 0 })
}

func coverage(ds *model.Dataset, builtAt time.Time, records map[string]CoverageRecords) *Coverage {
	cov := &Coverage{GeneratedAt: ds.GeneratedAt, BuiltAt: builtAt.Format(time.RFC3339Nano),
		Summary: CoverageSummary{Providers: len(ds.Providers), ProvidersBySource: map[string]int{model.SourceMeasured: 0},
			NAFieldsBySource: map[string]int{model.SourceMeasured: 0}, SnapshotPublished: ds.Snapshot.Available},
		Dataset: coverageEntity("dataset", "", "", ds.NA, false), Providers: []CoverageEntity{}, Targets: []CoverageEntity{},
		Scopes: []CoverageEntity{}, Profiles: []CoverageEntity{}, Records: records, Fields: map[string]CoverageFieldUse{}}
	cov.Summary.NAFields += len(ds.NA)
	for _, p := range ds.Providers {
		cov.Summary.ProvidersBySource[p.Source]++
		cov.Summary.NAFieldsBySource[p.Source] += len(p.NA)
		cov.Summary.NAFields += len(p.NA)
		cov.Providers = append(cov.Providers, coverageEntity(p.Slug, p.Name, p.Source, p.NA, true))
		seen := map[string]bool{}
		for _, path := range p.NA {
			pt := pattern(path)
			if seen[pt] {
				continue
			}
			seen[pt] = true
			f := cov.Fields[pt]
			f.Providers = append(f.Providers, p.Slug)
			f.Reason = reasonFor(path)
			cov.Fields[pt] = f
		}
	}
	for _, t := range ds.Targets {
		cov.Summary.NAFields += len(t.NA)
		cov.Targets = append(cov.Targets, coverageEntity(t.ID, model.SV(t.Label), t.Source, t.NA, false))
	}
	for _, s := range ds.Scopes {
		cov.Summary.NAFields += len(s.NA)
		cov.Scopes = append(cov.Scopes, coverageEntity(s.ID, model.SV(s.Label), "", s.NA, false))
	}
	for _, p := range ds.Profiles {
		cov.Summary.NAFields += len(p.NA)
		cov.Profiles = append(cov.Profiles, coverageEntity(p.ID, model.SV(p.Label), "", p.NA, false))
	}
	for _, r := range records {
		cov.Summary.NAFields += r.NACount
	}
	return cov
}
