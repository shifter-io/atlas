package build

import (
	"cmp"
	"net/http"
	"slices"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// /v1/events/stats (api/answers/events-stats.md): the live tape's figures of a
// range, from the event counts (store.EventStats), built again on every events
// refresh.

const eventsStatsPath = "/v1/events/stats"

func init() {
	RegisterAnswer(PageAnswer{Name: "events-stats", Root: eventsStatsPath, Events: true, Route: "GET " + eventsStatsPath,
		Build: buildEventsStats, Key: eventsStatsKey, InDataset: true,
		// The dataset file holds the ranges' bodies, not each network's.
		Request: func(p string) string { return queryRequest(p, eventsStatsPath, "range") },
		Samples: []string{eventsStatsPath + "?range=24h", eventsStatsPath + "?range=7d", eventsStatsPath},
		Cut: func(held map[string]any, path string) (any, bool) {
			body, ok := held[path]
			return body, ok
		}})
}

// eventsStatsKey is /v1/events/stats/<range>[/<network slug>].
func eventsStatsKey(r *http.Request) (string, error) {
	q, err := Query(r, "range", "network")
	if err != nil {
		return "", err
	}
	rangeID, err := QueryRange(q)
	if err != nil {
		return "", err
	}
	path := eventsStatsPath + "/" + rangeID
	if slug := q.Get("network"); slug != "" {
		if !slugPattern.MatchString(slug) {
			return "", InvalidFormat("network", slug, FormatSlug)
		}
		path += "/" + slug
	}
	return path, nil
}

// EventsStatsAnswer is /v1/events/stats: every figure covers [start, end).
type EventsStatsAnswer struct {
	Range    string         `json:"range"`
	Network  *string        `json:"network"`
	Start    string         `json:"start"`
	End      string         `json:"end"`
	Totals   StatsTotals    `json:"totals"`
	Pass     StatsPass      `json:"pass"`
	Buckets  []StatsBucket  `json:"buckets"`
	Heatmap  []StatsCell    `json:"heatmap"`
	Networks []StatsNetwork `json:"networks"`
}

// CategoryCounts is a count per live tape tab (store.Category).
type CategoryCounts struct {
	Census   int `json:"census"`
	Pulse    int `json:"pulse"`
	Incident int `json:"incident"`
	Audit    int `json:"audit"`
}

func (c *CategoryCounts) add(category string, n int) {
	switch category {
	case "census":
		c.Census += n
	case "pulse":
		c.Pulse += n
	case "audit":
		c.Audit += n
	default:
		c.Incident += n
	}
}

// StatsTotals is the range's events: by tab, good (signal > 0) and bad
// (signal < 0), the tracked networks that reported and how many are tracked.
type StatsTotals struct {
	Events     int            `json:"events"`
	ByCategory CategoryCounts `json:"byCategory"`
	Good       int            `json:"good"`
	Bad        int            `json:"bad"`
	Reporting  int            `json:"reporting"`
	Tracked    int            `json:"tracked"`
}

// StatsPass is the range's test requests and successes, from the network
// figures (/v1/networks/all's or the network's ranges), not event signals,
// and the rate's absolute 1-10 score (SUCCESS-SCORE-ALL-1).
type StatsPass struct {
	Requests     Num         `json:"requests"`
	Successes    Num         `json:"successes"`
	Rate         Num         `json:"rate"`
	SuccessScore MarketScore `json:"successScore"`
}

// StatsBucket is one sparkline bucket; Networks counts the tracked networks
// with an event in it.
type StatsBucket struct {
	Start      string         `json:"start"`
	Total      int            `json:"total"`
	ByCategory CategoryCounts `json:"byCategory"`
	Networks   int            `json:"networks"`
}

// StatsCell is one heatmap column.
type StatsCell struct {
	Start      string         `json:"start"`
	ByCategory CategoryCounts `json:"byCategory"`
}

// StatsNetwork is one tracked network's rail row; Recent is the signals of its
// newest store.RecentSignals events of the range, oldest first.
type StatsNetwork struct {
	Slug   string  `json:"slug"`
	Name   string  `json:"name"`
	Events int     `json:"events"`
	Good   int     `json:"good"`
	Bad    int     `json:"bad"`
	LastAt *string `json:"lastAt"`
	Recent []int16 `json:"recent"`
}

// statsRange is a range's span and its bucket and heatmap counts.
type statsRange struct {
	id             string
	span           time.Duration
	buckets, cells int
}

var statsRanges = []statsRange{{"24h", 24 * time.Hour, 24, 48}, {"7d", 7 * 24 * time.Hour, 28, 56}, {"30d", store.StatsDays * 24 * time.Hour, 30, 60}}

// statsNet is a tracked network: a catalog network with a framework provider.
type statsNet struct{ slug, name, provider string }

// statsInput is what every body of one build reads.
type statsInput struct {
	stats  *store.EventStats
	byProv map[string][]*store.EventBucket
	recent map[string][]store.RecentSignal // newest first
	pages  *Pages
}

// buildEventsStats is every range's answer for all networks and for each
// tracked network (network=), by stored path.
func buildEventsStats(c *Context) (map[string]any, error) {
	in := statsInput{stats: &c.Events.Stats, byProv: map[string][]*store.EventBucket{}, recent: map[string][]store.RecentSignal{}, pages: c.Pages}
	all := make([]*store.EventBucket, len(in.stats.Buckets))
	for i := range in.stats.Buckets {
		b := &in.stats.Buckets[i]
		all[i] = b
		if b.Provider != nil {
			in.byProv[*b.Provider] = append(in.byProv[*b.Provider], b)
		}
	}
	for _, r := range in.stats.Recent {
		in.recent[r.Provider] = append(in.recent[r.Provider], r)
	}
	for p, rs := range in.recent {
		slices.SortFunc(rs, func(a, b store.RecentSignal) int { return cmp.Or(b.At.Compare(a.At), cmp.Compare(b.ID, a.ID)) })
		in.recent[p] = rs[:min(len(rs), store.RecentSignals)]
	}
	nets := trackedNets(c)
	out := map[string]any{}
	for _, rg := range statsRanges {
		out[eventsStatsPath+"/"+rg.id] = in.body(rg, all, nets, nil, in.pass(rg.id, nil))
		for i := range nets {
			n := &nets[i]
			out[eventsStatsPath+"/"+rg.id+"/"+n.slug] = in.body(rg, in.byProv[n.provider], nets[i:i+1], &n.slug, in.pass(rg.id, n))
		}
	}
	return out, nil
}

// trackedNets is every catalog network with a framework provider, in the
// networks answer's order.
func trackedNets(c *Context) []statsNet {
	provider := map[string]string{}
	for p, slug := range c.Events.text.slugs {
		provider[slug] = p
	}
	out := []statsNet{}
	if c.Pages == nil {
		return out
	}
	for _, r := range c.Pages.Networks.Networks {
		if p, ok := provider[r.Slug]; ok {
			out = append(out, statsNet{r.Slug, model.SV(r.Name), p})
		}
	}
	return out
}

// pass is the range's requests, successes and rate of the all-networks
// answer, or of every kind of network n, with the rate's score.
func (in *statsInput) pass(rangeID string, n *statsNet) StatsPass {
	p := in.passFigures(rangeID, n)
	p.SuccessScore = absoluteScore(p.Rate, PassScoreBasis)
	return p
}

// passFigures is pass without its score.
func (in *statsInput) passFigures(rangeID string, n *statsNet) StatsPass {
	p := StatsPass{Requests: nan, Successes: nan, Rate: nan}
	if in.pages == nil {
		return p
	}
	if n == nil {
		if a := in.pages.Network[AllNetworks+"/"+AllNetworks]; a != nil && a.Ranges[rangeID] != nil {
			t := a.Ranges[rangeID]
			p.Requests, p.Successes, p.Rate = t.Requests, t.Successes, t.SuccessRate
		}
		return p
	}
	for _, r := range in.pages.Networks.Networks {
		for _, kind := range r.Kinds {
			if a := in.pages.Network[r.Slug+"/"+kind]; r.Slug == n.slug && a != nil && a.Ranges[rangeID] != nil {
				p.Requests, p.Successes = addNum(p.Requests, a.Ranges[rangeID].Requests), addNum(p.Successes, a.Ranges[rangeID].Successes)
			}
		}
	}
	p.Rate = ratio(p.Successes, p.Requests)
	return p
}

// body is one range's answer over rows (the network's own for network=),
// with nets as its rail.
func (in *statsInput) body(rg statsRange, rows []*store.EventBucket, nets []statsNet, network *string, pass StatsPass) *EventsStatsAnswer {
	end := in.stats.End
	start := end.Add(-rg.span)
	a := &EventsStatsAnswer{Range: rg.id, Network: network, Start: JSISO(start), End: JSISO(end), Pass: pass,
		Buckets: make([]StatsBucket, rg.buckets), Heatmap: make([]StatsCell, rg.cells), Networks: []StatsNetwork{}}
	step, cell := rg.span/time.Duration(rg.buckets), rg.span/time.Duration(rg.cells)
	for i := range a.Buckets {
		a.Buckets[i].Start = JSISO(start.Add(time.Duration(i) * step))
	}
	for i := range a.Heatmap {
		a.Heatmap[i].Start = JSISO(start.Add(time.Duration(i) * cell))
	}
	rail := map[string]*StatsNetwork{}
	order := make([]*StatsNetwork, len(nets))
	for i, n := range nets {
		order[i] = &StatsNetwork{Slug: n.slug, Name: n.name, Recent: in.recentOf(n.provider, start)}
		rail[n.provider] = order[i]
	}
	seen := make([]map[string]bool, rg.buckets)
	for _, b := range rows {
		if b.Start.Before(start) || !b.Start.Before(end) {
			continue
		}
		i, j := int(b.Start.Sub(start)/step), int(b.Start.Sub(start)/cell)
		a.Totals.add(b)
		a.Buckets[i].Total += b.Events
		a.Buckets[i].ByCategory.add(b.Category, b.Events)
		a.Heatmap[j].ByCategory.add(b.Category, b.Events)
		if n := railOf(rail, b); n != nil {
			n.add(b)
			if seen[i] == nil {
				seen[i] = map[string]bool{}
			}
			seen[i][n.Slug] = true
		}
	}
	for i := range a.Buckets {
		a.Buckets[i].Networks = len(seen[i])
	}
	a.Totals.Tracked = len(nets)
	for _, n := range order {
		if n.Events > 0 {
			a.Totals.Reporting++
		}
		a.Networks = append(a.Networks, *n)
	}
	slices.SortStableFunc(a.Networks, func(x, y StatsNetwork) int { return cmp.Compare(y.Events, x.Events) })
	return a
}

func railOf(rail map[string]*StatsNetwork, b *store.EventBucket) *StatsNetwork {
	if b.Provider == nil {
		return nil
	}
	return rail[*b.Provider]
}

func (t *StatsTotals) add(b *store.EventBucket) {
	t.Events += b.Events
	t.ByCategory.add(b.Category, b.Events)
	t.Good, t.Bad = t.Good+b.Good, t.Bad+b.Bad
}

func (n *StatsNetwork) add(b *store.EventBucket) {
	n.Events, n.Good, n.Bad = n.Events+b.Events, n.Good+b.Good, n.Bad+b.Bad
	if at := JSISO(b.LastAt); n.LastAt == nil || at > *n.LastAt {
		n.LastAt = &at
	}
}

// recentOf is the provider's newest signals from start, oldest first.
func (in *statsInput) recentOf(provider string, start time.Time) []int16 {
	out := []int16{}
	rs := in.recent[provider]
	for i := len(rs) - 1; i >= 0; i-- {
		if !rs[i].At.Before(start) {
			out = append(out, rs[i].Signal)
		}
	}
	return out
}
