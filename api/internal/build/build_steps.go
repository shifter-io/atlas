package build

import (
	"sort"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

// naManifests fills the N/A manifests of the providers, targets, scopes and
// profiles.
func (b *builder) naManifests() error {
	ds := b.ds
	var err error
	for _, e := range b.entries {
		declared := append([]string{}, e.Declared...)
		declared = append(declared, "overlap.likelyUpstream")
		for _, k := range e.P.ProductMeasurements.Keys {
			declared = append(declared, "productMeasurements."+k+".overlap.likelyUpstream")
		}
		e.P.NA, err = naOf(e.P, declared, true, "framework")
		if err != nil {
			return err
		}
		e.P.NA = withoutPublishedEmpties(e)
	}
	// After the N/A manifests: a coverage row's nulls are the row's own
	// unknown fields, not gaps of the provider (FIELDS.md).
	addCoverageEvidence(b.entries, b.in.Snapshot)
	for i := range ds.Targets {
		if ds.Targets[i].NA, err = naOf(ds.Targets[i], nil, false); err != nil {
			return err
		}
	}
	for i := range ds.Scopes {
		if ds.Scopes[i].NA, err = naOf(ds.Scopes[i], nil, false); err != nil {
			return err
		}
	}
	for i := range ds.Profiles {
		if ds.Profiles[i].NA, err = naOf(ds.Profiles[i], nil, false); err != nil {
			return err
		}
	}
	return nil
}

// recordCoverage fills the records' N/A manifests and counts them per record
// kind for the coverage report.
func (b *builder) recordCoverage() error {
	ds := b.ds
	b.records = map[string]CoverageRecords{}
	var err error
	for i := range ds.Reviews {
		if ds.Reviews[i].NA, err = naOf(ds.Reviews[i], nil, false); err != nil {
			return err
		}
		b.countRecords("reviews", ds.Reviews[i].NA)
	}
	for i := range ds.CommunityRuns {
		if ds.CommunityRuns[i].NA, err = naOf(ds.CommunityRuns[i], nil, false); err != nil {
			return err
		}
		b.countRecords("communityRuns", ds.CommunityRuns[i].NA)
	}
	for i := range ds.Changelog {
		if ds.Changelog[i].NA, err = naOf(ds.Changelog[i], nil, false); err != nil {
			return err
		}
		b.countRecords("changelog", ds.Changelog[i].NA)
	}
	for i := range ds.Tape {
		if ds.Tape[i].NA, err = naOf(ds.Tape[i], nil, false); err != nil {
			return err
		}
		b.countRecords("tape", ds.Tape[i].NA)
	}
	for _, name := range []string{"reviews", "communityRuns", "changelog", "tape"} {
		if _, ok := b.records[name]; !ok {
			b.records[name] = CoverageRecords{Fields: map[string]int{}}
		}
	}
	return nil
}

func (b *builder) countRecords(name string, na []string) {
	r := b.records[name]
	if r.Fields == nil {
		r.Fields = map[string]int{}
	}
	r.Count++
	r.NACount += len(na)
	for _, p := range na {
		r.Fields[pattern(p)]++
	}
	b.records[name] = r
}

// datasetNA fills the dataset's own N/A manifest and the score leaderboard.
func (b *builder) datasetNA() error {
	ds := b.ds
	meta := struct {
		SchemaVersion    string               `json:"schemaVersion"`
		GeneratedAt      string               `json:"generatedAt"`
		Seed             Num                  `json:"seed"`
		RunID            model.Str            `json:"runId"`
		CollectorVersion model.Str            `json:"collectorVersion"`
		WindowHours      Num                  `json:"windowHours"`
		Weights          model.Breakdown      `json:"weights"`
		Totals           model.FleetTotals    `json:"totals"`
		MeasuredTotals   model.FleetTotals    `json:"measuredTotals"`
		MeasuredMarket   model.MeasuredMarket `json:"measuredMarket"`
		Snapshot         model.SnapshotMeta   `json:"snapshot"`
	}{ds.SchemaVersion, ds.GeneratedAt, ds.Seed, ds.RunID, ds.CollectorVersion, ds.WindowHours, ds.Weights, ds.Totals, ds.MeasuredTotals, ds.MeasuredMarket, ds.Snapshot}
	// The dataset has no seed by design (only the removed demo fixtures had one).
	meta.Seed = 0
	var err error
	if ds.NA, err = naOf(meta, []string{}, false); err != nil {
		return err
	}
	b.res.Dataset = ds

	// Leaderboard: score rank, unranked last (stable).
	leader := append([]*model.Provider{}, ds.Providers...)
	sort.SliceStable(leader, func(i, j int) bool {
		x, y := leader[i].Score.Rank, leader[j].Score.Rank
		if x.OK() != y.OK() {
			return x.OK()
		}
		return x-y < 0
	})
	b.leader = leader
	return nil
}

// details fills every provider's detail: its windows per product and its own
// records.
func (b *builder) details() {
	ds := b.ds
	for _, e := range b.entries {
		p := e.P
		d := &ProviderDetail{Provider: p, Windows: map[string]map[string]*WindowStats{}, Reviews: []model.UserReview{},
			CommunityRuns: []model.CommunityRun{}, Changelog: []model.ChangeEvent{}, Tape: []model.TapeEvent{}, GeneratedAt: ds.GeneratedAt}
		for _, k := range p.ProductMeasurements.Keys {
			view := providerForProduct(p, k)
			d.Windows[k] = map[string]*WindowStats{}
			for _, r := range Ranges {
				d.Windows[k][r.ID] = windowStats(view, e.Snap[k], r.ID)
			}
		}
		for _, r := range ds.Reviews {
			if r.ProviderSlug == p.Slug && r.Verification != "pending" {
				d.Reviews = append(d.Reviews, r)
			}
		}
		for _, r := range ds.CommunityRuns {
			if r.ProviderSlug == p.Slug {
				d.CommunityRuns = append(d.CommunityRuns, r)
			}
		}
		for _, r := range ds.Changelog {
			if r.ProviderSlug != nil && *r.ProviderSlug == p.Slug {
				d.Changelog = append(d.Changelog, r)
			}
		}
		for _, r := range ds.Tape {
			if r.ProviderSlug == p.Slug {
				d.Tape = append(d.Tape, r)
			}
		}
		b.res.Details[p.Slug] = d
	}
}

// assemble builds the pages, lists, search index and coverage report.
func (b *builder) assemble() {
	res, ds, s, in := b.res, b.ds, b.s, b.in
	res.Pages = buildPages(ds.GeneratedAt, b.entries, b.stats, res.Details, s, in.Snapshot)
	res.Pages.Published = publishedProducts(s, b.entries, in.Providers)
	ds.Pages = datasetPages(res.Pages)
	ds.Lists = datasetLists(res.Pages, s, b.entries, in.Snapshot)
	res.Search = buildSearchIndex(b.leader, ds.Targets, ds.GeneratedAt)
	res.Coverage = coverage(ds, res.BuiltAt, b.records)
}
