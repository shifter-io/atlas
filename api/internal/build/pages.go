package build

import (
	"sort"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// MaxAnswerBytes bounds every per-page answer (identity bytes). The server
// tests render a QA-1-sized build and fail when one is larger.
const MaxAnswerBytes = 200 << 10

// PageSize is how many rows one page of a long list carries.
const PageSize = 50

// TopCountries is how many countries a network answer lists per range.
const TopCountries = 50

// Pages is the per-page answers of one build: one row per network for the
// overview, one answer per network and per country, and the long place lists
// that /v1/networks/{slug}/places and /v1/countries/{cc}/cities page through.
// Everything is computed here, once per build; a request only looks it up or
// cuts a page from a precomputed list.
type Pages struct {
	Networks  NetworksAnswer
	Network   map[string]*NetworkAnswer // by slug + "/" + kind
	Primary   map[string]string         // slug -> its headline kind
	Countries CountriesAnswer
	Country   map[string]*CountryAnswer  // by upper-case country code
	Rankings  map[string]*RankingsAnswer // by range
	// RankingsByKind is each proxy kind's ranking, by range + "/" + kind.
	RankingsByKind map[string]*RankingsAnswer
	Charts         map[string]*ChartAnswer // by slug + "/" + kind + "/" + range
	// Published is every product group of the published snapshot and the
	// network that should serve it. It is not an answer: the server checks
	// after each build that each one has its network pages.
	Published []PublishedProduct
	// Registered is every registered answer's body by its stored path
	// (answers_registry.go); Unbuilt is each answer left out, with why.
	// Neither is part of the encoded Pages: each body is its own answer.
	Registered map[string]any    `json:"-"`
	Unbuilt    map[string]string `json:"-"`
	// Held is, per registered answer with Cut, what its Build returned, by
	// path: the server cuts that answer's bodies from it on request.
	Held map[string]map[string]any `json:"-"`
	// Changelog is each range's change record (changelog.go), the body of
	// /v1/changelog; changeEntries is every 30d entry before the cut, which
	// the network answers' changelog[] is taken from.
	Changelog     map[string]*ChangeRecord `json:"-"`
	changeEntries []ChangeEntry
	// lists and coverage are read from the network lists (placelists.go).
	lists    map[listKey]*placeIndex
	coverage map[listKey]*coverageList
	sources  map[placeList][]placeSource // every network's list of a range and region or city dimension
	// standings is the rankable places of the network lists (place_scores.go).
	standings map[placeScoreKey]placeStanding
}

// PublishedProduct is one product group of the published snapshot.
type PublishedProduct struct {
	ID       string
	Provider string // the framework provider id
	Kind     string // its category, as the network answers are keyed
	// Slug is the network that shows it; empty when no network does.
	Slug string
	// Hidden is a provider kept off the site on purpose (a catalog row with
	// listed = false); it has no pages and that is not a gap.
	Hidden bool
}

// publishedProducts pairs each snapshot product group with its network.
func publishedProducts(s *snap.Snapshot, entries []*entry, rows []store.ProviderRow) []PublishedProduct {
	if s == nil {
		return nil
	}
	slugOf := map[string]string{}
	for _, e := range entries {
		if e.P.Framework != nil {
			slugOf[e.P.Framework.Provider] = e.P.Slug
		}
	}
	hidden := map[string]bool{}
	for _, r := range rows {
		if r.FrameworkProvider != nil && !r.Listed {
			hidden[*r.FrameworkProvider] = true
		}
	}
	out := make([]PublishedProduct, 0, len(s.Products))
	for _, p := range s.Products {
		out = append(out, PublishedProduct{ID: p.ID, Provider: p.Provider, Kind: proxyKind(p.Kind), Slug: slugOf[p.Provider], Hidden: hidden[p.Provider]})
	}
	return out
}

// NetworkRow is one network in the overview: the headline numbers the site's
// leaderboard shows.
type NetworkRow struct {
	Slug        string   `json:"slug"`
	Name        string   `json:"name"`
	Source      string   `json:"source"`
	Kind        string   `json:"kind"`
	Kinds       []string `json:"kinds"`
	Rank        Num      `json:"rank"`
	Score       Num      `json:"score"`
	UniqueIps   Num      `json:"uniqueIps"` // the headline group's distinct IPs (30d)
	Countries   Num      `json:"countries"`
	LatencyP50  Num      `json:"latencyP50"`
	SuccessRate Num      `json:"successRate"`
	// SuccessScore is the headline group's 30-day overall success score;
	// null when the snapshot has none.
	SuccessScore *model.SuccessScore `json:"successScore"`
}

// NetworksAnswer is /v1/networks.
type NetworksAnswer struct {
	GeneratedAt string       `json:"generatedAt"`
	Networks    []NetworkRow `json:"networks"`
}

// PlaceRow is one country, region, city or ASN of a network (or, in a
// country's city list, of one network in that country). Rows of several
// product groups that share a place add their requests and keep the latency
// p50 of the row with the most successes (as the place ranks do). IpsSum adds
// each group's distinct IPs: an IP seen by two groups (two requested
// countries) counts twice, so it is an upper bound of the distinct IPs, and
// IpsSumRank ranks that sum.
type PlaceRow struct {
	Network     string      `json:"network,omitempty"`
	NetworkName string      `json:"networkName,omitempty"`
	Kind        string      `json:"kind,omitempty"`
	Key         string      `json:"key"`
	CountryCode model.Str   `json:"countryCode,omitempty"`
	Country     model.Str   `json:"country,omitempty"`
	RegionCode  model.Str   `json:"regionCode,omitempty"`
	Region      model.Str   `json:"region,omitempty"`
	City        model.Str   `json:"city,omitempty"`
	Asn         *int64      `json:"asn,omitempty"`
	AsnName     model.Str   `json:"asnName,omitempty"`
	IpsSum      Num         `json:"ipsSum"`
	Requests    Num         `json:"requests"`
	Successes   Num         `json:"successes"`
	SuccessRate Num         `json:"successRate"`
	LatencyP50  Num         `json:"latencyP50"`
	IpsSumRank  *model.Rank `json:"ipsSumRank,omitempty"`
	LatencyRank *model.Rank `json:"latencyRank,omitempty"`
	// SuccessScore is the row's 1-10 success score among the networks of its
	// kind at the place: on a region or city page (placeScores) and on a
	// network's own lists and top countries (the network's, placeScore). Not
	// written on a country's answer (in the dataset file, which has no room
	// for them) and lists, nor on the all-networks lists.
	SuccessScore *model.MetricScore `json:"successScore,omitempty"`
	most         Num
}

// DayRow is one UTC day of a network's 30-day window, summed over its
// product groups. IpsSum adds each group's distinct IPs (an upper bound of
// the day's distinct IPs, not a distinct count); UniqueIps is the day's
// distinct exit IPs where they are known (one product group, or the
// all-networks answer), else null.
type DayRow struct {
	Day         string `json:"day"`
	Requests    Num    `json:"requests"`
	Successes   Num    `json:"successes"`
	SuccessRate Num    `json:"successRate"`
	// SuccessScore is the day's overall success score over every target,
	// the window's rule (setDayScores); null for the all-networks answer, or
	// a day the snapshot has none for.
	SuccessScore *DayScore `json:"successScore"`
	IpsSum       Num       `json:"ipsSum"`
	UniqueIps    Num       `json:"uniqueIps"`
}

// RangeTotals sums a network's product groups over one range; IpsSum adds
// each group's distinct IPs (an upper bound, not a distinct count). UniqueIps
// and the latency percentiles cannot be added up: they are the group's own
// with one product group, the snapshot's every-group fleet for the
// all-networks answer, and null otherwise.
type RangeTotals struct {
	Requests    Num `json:"requests"`
	Successes   Num `json:"successes"`
	SuccessRate Num `json:"successRate"`
	// SuccessScore is the network's overall success score in the range (its
	// kind's window); null for the all-networks answer or without one.
	SuccessScore *model.SuccessScore `json:"successScore"`
	IpsSum       Num                 `json:"ipsSum"`
	UniqueIps    Num                 `json:"uniqueIps"`
	// DatacenterIps is the one group's stats datacenterIps, as UniqueIps.
	DatacenterIps Num            `json:"datacenterIps"`
	LatencyP50    Num            `json:"latencyP50"`
	LatencyP90    Num            `json:"latencyP90"`
	LatencyP99    Num            `json:"latencyP99"`
	ExitCountries int            `json:"exitCountries"`
	Countries     []PlaceRow     `json:"countries"`
	Lists         map[string]int `json:"lists"`
}

// NetworkAnswer is /v1/networks/{slug}: that network's daily numbers and its
// top countries per range; the full lists are paged by .../places.
type NetworkAnswer struct {
	GeneratedAt        string                  `json:"generatedAt"`
	Network            NetworkRow              `json:"network"`
	Kind               string                  `json:"kind"`
	ProductGroups      int                     `json:"productGroups"`
	RequestedCountries []string                `json:"requestedCountries"`
	Days               []DayRow                `json:"days"`
	Ranges             map[string]*RangeTotals `json:"ranges"`
	Networks           int                     `json:"networks,omitempty"` // all-networks answer only: networks covered
	// The network page's profile (null for the all-networks answer), the
	// category's window statistics per range without histories, and the
	// network's newest published reviews, community runs, changelog and tape
	// rows (at most RecordCap each).
	Profile       *NetworkProfile        `json:"profile"`
	Stats         map[string]*RangeStats `json:"stats"`
	Reviews       []model.UserReview     `json:"reviews"`
	CommunityRuns []model.CommunityRun   `json:"communityRuns"`
	Changelog     []ChangeEntry          `json:"changelog"`
	Tape          []model.TapeEvent      `json:"tape"`
	Records       RecordCounts           `json:"records"` // full lengths of the four lists, each capped at RecordCap
	// NAList lists the answer's own nulls and empty reviews and runs; the
	// profile, each range's stats and each record carry their own.
	NAList
}

// CountryIndexRow is one country of /v1/countries (30-day window); IpsSum
// adds the networks' place rows (an upper bound of the distinct IPs).
type CountryIndexRow struct {
	CountryCode string    `json:"countryCode"`
	Country     model.Str `json:"country"`
	Networks    int       `json:"networks"`
	IpsSum      Num       `json:"ipsSum"`
}

// CountriesAnswer is /v1/countries.
type CountriesAnswer struct {
	GeneratedAt string            `json:"generatedAt"`
	Countries   []CountryIndexRow `json:"countries"`
}

// CountryAnswer is /v1/countries/{cc}: that country's row of every network per
// range, ranked among the networks of the same kind; its cities are paged by
// .../cities.
type CountryAnswer struct {
	GeneratedAt string                `json:"generatedAt"`
	CountryCode string                `json:"countryCode"`
	Country     model.Str             `json:"country"`
	Ranges      map[string][]PlaceRow `json:"ranges"`
	Cities      map[string]int        `json:"cities"`
}

// buildPages computes every per-page answer of a build; it fails when a
// network list's rows cannot be read.
func buildPages(generatedAt string, entries []*entry, stats map[string]map[string]*WindowStats, details map[string]*ProviderDetail,
	s *snap.Snapshot, row *store.SnapshotRow) (*Pages, error) {
	nr := &netRead{ctx: netRowsCtx(row), row: row}
	pg := &Pages{Network: map[string]*NetworkAnswer{}, Primary: map[string]string{}, Country: map[string]*CountryAnswer{},
		Charts: map[string]*ChartAnswer{}, lists: map[listKey]*placeIndex{}, coverage: map[listKey]*coverageList{}}
	pg.Networks = NetworksAnswer{GeneratedAt: generatedAt, Networks: []NetworkRow{}}
	groups := groupsByProvider(s)
	byCountry := map[string]map[string][]PlaceRow{} // cc -> range -> one row per network and kind
	country := countryLists{map[placeList][]placeSource{}, map[listKey]int{}, map[listKey][]PlaceRow{}}
	rated := placeRatings{}
	for _, e := range entries {
		head := networkRow(e)
		pg.Networks.Networks = append(pg.Networks.Networks, head)
		pg.Primary[e.P.Slug] = e.P.PrimaryKind
		for _, kind := range head.Kinds {
			mine := groupsOfKind(e, groups, kind)
			ans, places, srcs := networkAnswer(generatedAt, head, kind, mine, nr)
			ans.setRangeScores(e.Snap[kind])
			pg.Network[e.P.Slug+"/"+kind] = ans
			pg.addCoverage(e, kind, nr)
			for key, rows := range places {
				pg.lists[listKey{scope: e.P.Slug + "/" + kind, rangeID: key.rangeID, dimension: key.dimension}] = pg.index(rows, srcs[key.rangeID], key.dimension)
				rated.add(e.P.Slug, kind, key, rows)
				switch {
				case key.dimension == "country":
					addCountryRows(byCountry, key.rangeID, rows, head, kind)
				case key.dimension != "asn" && len(srcs[key.rangeID]) > 0:
					country.add(key, srcs[key.rangeID], rows)
				}
			}
		}
	}
	country.indexes(pg.lists)
	pg.sources = country.srcs
	pg.addSuccessScores(rated)
	sort.SliceStable(pg.Networks.Networks, func(i, j int) bool { return rankLess(pg.Networks.Networks[i], pg.Networks.Networks[j]) })
	pg.countryAnswers(generatedAt, byCountry)
	if pg.Primary[AllNetworks] == "" {
		ans, places, srcs := allAnswer(generatedAt, s, nr)
		pg.Network[AllNetworks+"/"+AllNetworks], pg.Primary[AllNetworks] = ans, AllNetworks
		for key, rows := range places {
			pg.lists[listKey{scope: AllNetworks + "/" + AllNetworks, rangeID: key.rangeID, dimension: key.dimension}] = pg.index(rows, srcs[key.rangeID], key.dimension)
		}
		pg.allCharts(generatedAt, s)
	}
	pg.addDetails(generatedAt, details)
	return pg, nr.err
}

// index is a list of rows in order: its count and first page and the
// sources it is merged from.
func (pg *Pages) index(rows []PlaceRow, srcs []placeSource, dimension string) *placeIndex {
	return &placeIndex{srcs: append([]placeSource{}, srcs...), dimension: dimension, total: len(rows), first: firstPage(rows)}
}

func networkRow(e *entry) NetworkRow {
	p := e.P
	kinds := append([]string{}, p.ProductMeasurements.Keys...)
	if len(kinds) == 0 {
		kinds = []string{p.PrimaryKind}
	}
	return NetworkRow{Slug: p.Slug, Name: p.Name, Source: p.Source, Kind: p.PrimaryKind, Kinds: kinds, Rank: p.Score.Rank,
		Score: p.Score.Composite, UniqueIps: p.Pool.UniqueIps30d, Countries: p.Pool.Countries,
		LatencyP50: p.Performance.LatencyP50, SuccessRate: p.Performance.SuccessRate, SuccessScore: p.SuccessScore}
}

// rankLess orders networks by rank (unranked last), then name.
func rankLess(a, b NetworkRow) bool {
	if a.Rank.OK() != b.Rank.OK() {
		return a.Rank.OK()
	}
	if a.Rank.OK() && a.Rank != b.Rank {
		return a.Rank < b.Rank
	}
	return a.Name < b.Name
}

func groupsByProvider(s *snap.Snapshot) map[string][]*snap.Product {
	out := map[string][]*snap.Product{}
	if s == nil {
		return out
	}
	for i := range s.Products {
		p := &s.Products[i]
		out[p.Provider] = append(out[p.Provider], p)
	}
	return out
}

// groupsOfKind is every published product group of the entry's network and
// category: one per cohort (daily tests make one cohort per requested country).
func groupsOfKind(e *entry, groups map[string][]*snap.Product, kind string) []*snap.Product {
	if e.P.Framework == nil {
		return nil
	}
	out := []*snap.Product{}
	for _, p := range groups[e.P.Framework.Provider] {
		if proxyKind(p.Kind) == kind {
			out = append(out, p)
		}
	}
	return out
}
