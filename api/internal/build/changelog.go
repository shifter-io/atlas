package build

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"sort"
	"strings"

	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// /v1/changelog?range= (api/answers/changelog.md): every network's changes
// in the website's ChangeRecord shape (changelog-data.ts), newest first, and
// the rank movers. Built once per build for each range; the network answers'
// changelog[] carries the same entries (Pages.addNetworkChangelog).

const changelogPath = "/v1/changelog"

func init() {
	RegisterAnswer(PageAnswer{Name: "changelog", Route: "GET " + changelogPath, Build: buildChangelog, InDataset: true,
		Request: func(p string) string { return queryRequest(p, changelogPath, "range") },
		Samples: []string{changelogPath, changelogPath + "?range=24h", changelogPath + "?range=7d"},
		Key: func(r *http.Request) (string, error) {
			q, err := Query(r, "range")
			if err != nil {
				return "", err
			}
			rangeID, err := QueryRange(q)
			return changelogPath + "/" + rangeID, err
		}})
}

// The changelog's limits and thresholds (api/answers/changelog.md).
const (
	// ChangelogWeekSeconds is the record's weekSeconds: the span the page's
	// week counts cover.
	ChangelogWeekSeconds = 7 * 86400
	// ChangelogMaxEntries is how many of the newest entries one body keeps,
	// and ChangelogEntryBytes how many bytes they may take together (the
	// movers and the rest of the body fit in what MaxAnswerBytes leaves);
	// older entries are cut.
	ChangelogMaxEntries = 300
	ChangelogEntryBytes = MaxAnswerBytes - 24<<10
	// An incident is minor when it lasts under IncidentMinorMinutes and
	// takes under IncidentMinorShare of the tested countries, major when it
	// lasts over IncidentMajorMinutes or takes over IncidentMajorShare.
	IncidentMinorMinutes = 30
	IncidentMinorShare   = 0.05
	IncidentMajorMinutes = 120
	IncidentMajorShare   = 0.25
	// A performance entry needs the success rate to move at least
	// PerformanceSuccessPts points or the median latency PerformanceLatencyPct
	// percent; a pool entry the unique IPs PoolChangePct percent; an
	// authenticity entry the geo accuracy AuthenticityPts points.
	PerformanceSuccessPts = 2.0
	PerformanceLatencyPct = 15.0
	PoolChangePct         = 10.0
	AuthenticityPts       = 2.0
)

// ChangeRecord is changelog-data.ts ChangeRecord.
type ChangeRecord struct {
	GeneratedAt string        `json:"generatedAt"`
	RunID       model.Str     `json:"runId"`
	WeekSeconds int           `json:"weekSeconds"`
	Entries     []ChangeEntry `json:"entries"`
	Movers      []Mover       `json:"movers"`
}

// ChangeEntry is changelog-data.ts ChangeEntry. Its nulls are the shape's
// own (a fleet-wide entry has no network, a change no incident), so it has
// no na list. It has no English title, detail or unit and no delta beside
// its values (CHANGELOG-TEXT-REMOVE-1, #990): the website writes the line
// from Event and Values.
type ChangeEntry struct {
	ID   string `json:"id"`
	Kind string `json:"kind"`
	// Event is the change's code (CHANGELOG-EVENT-1, the change_event set):
	// a move's code, incident or catalog.
	Event      string         `json:"event"`
	Network    *ChangeNetwork `json:"network"`
	Severity   string         `json:"severity"`
	AgoSeconds Num            `json:"agoSeconds"`
	Source     string         `json:"source"`
	Incident   *ChangeOutage  `json:"incident"`
	// Values is the entry's numbers and codes, the keys of its kind
	// (changeValueKeys); NAReasons the reason of each null one, by its path
	// ("values.plan"), left out when there is none.
	Values    ChangeValues      `json:"values"`
	NAReasons map[string]string `json:"naReasons,omitempty"`
}

// ChangeNetwork names an entry's network.
type ChangeNetwork struct {
	Slug string `json:"slug"`
	Name string `json:"name"`
}

// ChangeOutage is an incident entry's incident: its severity code and
// profile.incidents[]'s facts (no English scope or summary since
// API-PROSE-REMOVAL-1, #962; no trough success rate since
// SUCCESS-RATE-REMOVE-1, #900).
type ChangeOutage struct {
	Severity        string `json:"severity"`
	Impact          Num    `json:"impact"`
	DurationMinutes Num    `json:"durationMinutes"`
	model.IncidentFacts
}

// Mover is changelog-data.ts Mover: a network whose balanced-score rank
// (30d) differs from its previous rank.
type Mover struct {
	Slug         string  `json:"slug"`
	Name         string  `json:"name"`
	Source       string  `json:"source"`
	Rank         Num     `json:"rank"`
	PreviousRank Num     `json:"previousRank"`
	Delta        Num     `json:"delta"`
	ReasonID     *string `json:"reasonId"`
}

func buildChangelog(c *Context) (map[string]any, error) {
	if c.Pages == nil || c.Pages.Changelog == nil {
		return nil, errors.New("no changelog built")
	}
	out := map[string]any{}
	for rangeID, rec := range c.Pages.Changelog {
		out[changelogPath+"/"+rangeID] = rec
	}
	return out, nil
}

// changeRecords is every range's record, with every entry (before the cut)
// for the network answers.
func changeRecords(c *Context) map[string]*ChangeRecord {
	out := map[string]*ChangeRecord{}
	var runID model.Str
	if c.Dataset != nil {
		runID = c.Dataset.RunID
	}
	for _, r := range Ranges {
		entries := changeEntries(c, r)
		rec := &ChangeRecord{GeneratedAt: c.GeneratedAt, RunID: runID, WeekSeconds: ChangelogWeekSeconds, Entries: newestEntries(entries)}
		rec.Movers = movers(c.Entries, rec.Entries)
		out[r.ID] = rec
		if r.ID == "30d" {
			c.Pages.changeEntries = entries
		}
	}
	return out
}

// changeEntries is every entry of range r, newest first.
func changeEntries(c *Context, r RangeMeta) []ChangeEntry {
	seconds := float64(r.Days * 86400)
	out := []ChangeEntry{}
	for _, e := range c.Entries {
		out = append(out, incidentEntries(e, c, seconds)...)
		out = append(out, windowEntries(e, c, r.ID)...)
	}
	out = append(out, priceEntries(c, seconds)...)
	out = append(out, catalogEntries(c, seconds)...)
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].AgoSeconds != out[j].AgoSeconds {
			return out[i].AgoSeconds < out[j].AgoSeconds
		}
		return out[i].ID < out[j].ID
	})
	return out
}

// newestEntries is the newest of entries (newest first) within
// ChangelogMaxEntries and ChangelogEntryBytes.
func newestEntries(entries []ChangeEntry) []ChangeEntry {
	size := 0
	for i, en := range entries {
		b, err := json.Marshal(en)
		if size += len(b) + 1; err != nil || i == ChangelogMaxEntries || size > ChangelogEntryBytes {
			return entries[:i]
		}
	}
	return entries
}

// movers is every network whose 30d balanced rank moved, the largest moves
// first; its reason is its newest non-incident entry in entries.
func movers(all []*entry, entries []ChangeEntry) []Mover {
	out := []Mover{}
	for _, e := range all {
		s := e.P.Score
		if !s.Rank.OK() || !s.PreviousRank.OK() || s.Rank == s.PreviousRank {
			continue
		}
		m := Mover{Slug: e.P.Slug, Name: e.P.Name, Source: e.P.Source, Rank: s.Rank, PreviousRank: s.PreviousRank, Delta: s.PreviousRank - s.Rank}
		for _, en := range entries {
			if en.Kind != "incident" && en.Network != nil && en.Network.Slug == e.P.Slug {
				m.ReasonID = &en.ID
				break
			}
		}
		out = append(out, m)
	}
	sort.SliceStable(out, func(i, j int) bool {
		if a, b := math.Abs(out[i].Delta.F()), math.Abs(out[j].Delta.F()); a != b {
			return a > b
		}
		return out[i].Slug < out[j].Slug
	})
	return out
}

// addChangelog builds the changelog records and gives every network answer
// its network's newest 30d entries (at most RecordCap) and their count.
func (pg *Pages) addChangelog(c *Context) error {
	pg.Changelog = changeRecords(c)
	return pg.addNetworkChangelog(pg.changeEntries)
}

// addNetworkChangelog gives every network answer its entries, their nulls
// listed in its na (listChangelog).
func (pg *Pages) addNetworkChangelog(entries []ChangeEntry) error {
	bySlug := map[string][]ChangeEntry{}
	for _, en := range entries {
		if en.Network != nil {
			bySlug[en.Network.Slug] = append(bySlug[en.Network.Slug], en)
		}
	}
	for key, ans := range pg.Network {
		slug, _, _ := strings.Cut(key, "/")
		mine := bySlug[slug]
		ans.Changelog = append([]ChangeEntry{}, newest(mine)...)
		ans.Records.Changelog = len(mine)
		if err := ans.listChangelog(); err != nil {
			return fmt.Errorf("network %s changelog: %w", key, err)
		}
	}
	return nil
}

// asOfAge is the seconds from at to the build's time, never below 0.
func asOfAge(generatedAt string, at float64) Num {
	return Num(math.Max(0, jsnum.Round((snap.Ms(generatedAt)-at)/1000)))
}
