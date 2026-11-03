package build

import (
	"encoding/json"
	"sort"

	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
	"github.com/Zeus-Development/proxy-index-website/api/internal/snap"
	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
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
	Snapshot  *SnapshotSummary           // nil while nothing is published
	Rankings  map[string]*RankingsAnswer // by range
	lists     map[listKey][]PlaceRow
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
	most        Num
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
	IpsSum      Num    `json:"ipsSum"`
	UniqueIps   Num    `json:"uniqueIps"`
}

// RangeTotals sums a network's product groups over one range; IpsSum adds
// each group's distinct IPs (an upper bound, not a distinct count). UniqueIps
// and the latency percentiles cannot be added up: they are the group's own
// with one product group, the snapshot's every-group fleet for the
// all-networks answer, and null otherwise.
type RangeTotals struct {
	Requests      Num            `json:"requests"`
	Successes     Num            `json:"successes"`
	SuccessRate   Num            `json:"successRate"`
	IpsSum        Num            `json:"ipsSum"`
	UniqueIps     Num            `json:"uniqueIps"`
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

// SnapshotSummary is /v1/snapshot: what the published calculation is (its id,
// times, readiness and methodology settings) and every network's peer scores,
// without the per-group windows (those are paged by the answers above).
type SnapshotSummary struct {
	Available        bool                   `json:"available"`
	SnapshotID       string                 `json:"snapshotId"`
	SchemaVersion    Num                    `json:"schemaVersion"`
	ImplementationID *string                `json:"implementationId"`
	AsOf             string                 `json:"asOf"`
	GeneratedAt      string                 `json:"generatedAt"`
	Readiness        string                 `json:"readiness"`
	RefreshSeconds   Num                    `json:"refreshSeconds"`
	ContentHash      string                 `json:"contentHash"`
	Methodology      json.RawMessage        `json:"methodology"`
	Periods          map[string]snap.Period `json:"periods"`
	ProductGroups    int                    `json:"productGroups"`
	Scores           []PeerScores           `json:"scores"`
}

// PeerScores is one network's headline product group scores per range and
// profile, as the snapshot publishes them.
type PeerScores struct {
	Network string                           `json:"network"`
	Kind    string                           `json:"kind"`
	Cohort  string                           `json:"cohort"`
	Ranges  map[string]map[string]snap.Score `json:"ranges"`
}

// buildPages computes every per-page answer of a build.
func buildPages(generatedAt string, entries []*entry, stats map[string]map[string]*WindowStats, s *snap.Snapshot, row *store.SnapshotRow) *Pages {
	pg := &Pages{Network: map[string]*NetworkAnswer{}, Primary: map[string]string{}, Country: map[string]*CountryAnswer{},
		lists: map[listKey][]PlaceRow{}}
	pg.Networks = NetworksAnswer{GeneratedAt: generatedAt, Networks: []NetworkRow{}}
	groups := groupsByProvider(s)
	byCountry := map[string]map[string][]PlaceRow{} // cc -> range -> one row per network and kind
	for _, e := range entries {
		head := networkRow(e)
		pg.Networks.Networks = append(pg.Networks.Networks, head)
		pg.Primary[e.P.Slug] = e.P.PrimaryKind
		for _, kind := range head.Kinds {
			mine := groupsOfKind(e, groups, kind)
			ans, places := networkAnswer(generatedAt, head, kind, mine, row)
			pg.Network[e.P.Slug+"/"+kind] = ans
			for key, rows := range places {
				pg.lists[listKey{scope: e.P.Slug + "/" + kind, rangeID: key.rangeID, dimension: key.dimension}] = rows
				if key.dimension == "country" {
					addCountryRows(byCountry, key.rangeID, rows, head, kind)
				}
				if key.dimension == "city" {
					pg.addCityRows(key.rangeID, rows, head, kind)
				}
			}
		}
	}
	sort.SliceStable(pg.Networks.Networks, func(i, j int) bool { return rankLess(pg.Networks.Networks[i], pg.Networks.Networks[j]) })
	pg.countryAnswers(generatedAt, byCountry)
	pg.Rankings = rankings(generatedAt, entries, stats)
	if pg.Primary[AllNetworks] == "" {
		ans, places := allAnswer(generatedAt, s, row)
		pg.Network[AllNetworks+"/"+AllNetworks], pg.Primary[AllNetworks] = ans, AllNetworks
		for key, rows := range places {
			pg.lists[listKey{scope: AllNetworks + "/" + AllNetworks, rangeID: key.rangeID, dimension: key.dimension}] = rows
		}
	}
	if s != nil {
		pg.Snapshot = snapshotSummary(s, row, entries)
	}
	return pg
}

func networkRow(e *entry) NetworkRow {
	p := e.P
	kinds := append([]string{}, p.ProductMeasurements.Keys...)
	if len(kinds) == 0 {
		kinds = []string{p.PrimaryKind}
	}
	return NetworkRow{Slug: p.Slug, Name: p.Name, Source: p.Source, Kind: p.PrimaryKind, Kinds: kinds, Rank: p.Score.Rank,
		Score: p.Score.Composite, UniqueIps: p.Pool.UniqueIps30d, Countries: p.Pool.Countries,
		LatencyP50: p.Performance.LatencyP50, SuccessRate: p.Performance.SuccessRate}
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

func snapshotSummary(s *snap.Snapshot, row *store.SnapshotRow, entries []*entry) *SnapshotSummary {
	out := &SnapshotSummary{Available: true, SnapshotID: s.SnapshotID, SchemaVersion: s.SchemaVersion, ImplementationID: s.ImplementationID,
		AsOf: s.AsOf, GeneratedAt: s.GeneratedAt, Readiness: s.Readiness, RefreshSeconds: s.RefreshSeconds, ContentHash: row.ContentHash,
		Methodology: s.Methodology.Raw, Periods: map[string]snap.Period{}, ProductGroups: len(s.Products), Scores: []PeerScores{}}
	// Every group shares the build's reporting periods.
	for _, p := range s.Products[:min(len(s.Products), 1)] {
		for label, w := range p.Windows {
			out.Periods[label] = w.Period
		}
	}
	for _, e := range entries {
		for _, kind := range e.P.ProductMeasurements.Keys {
			p := e.Snap[kind]
			if p == nil {
				continue
			}
			ps := PeerScores{Network: e.P.Slug, Kind: kind, Cohort: p.Cohort, Ranges: map[string]map[string]snap.Score{}}
			for _, r := range Ranges {
				ps.Ranges[r.ID] = p.Window(r.ID).Scores
			}
			out.Scores = append(out.Scores, ps)
		}
	}
	return out
}
