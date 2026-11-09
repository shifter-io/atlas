package build

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// statsEnd is the fixture's End: its newest bucket is 11:30 to 12:00.
var statsEnd = time.Date(2026, 11, 9, 12, 0, 0, 0, time.UTC)

// statsFixture is a build's events window and pages: alpha (provider a) and
// beta (provider b) tracked, gamma without a framework provider, events in
// three tabs over three days (and a product-less one), and pass figures.
func statsFixture() *Context {
	at := func(d time.Duration) time.Time { return statsEnd.Add(-d) }
	b := func(ago time.Duration, provider *string, category string, n, good, bad int) store.EventBucket {
		return store.EventBucket{Start: at(ago), Provider: provider, Category: category, Events: n, Good: good, Bad: bad, LastAt: at(ago).Add(10 * time.Minute)}
	}
	stats := store.EventStats{End: statsEnd, Buckets: []store.EventBucket{
		b(30*time.Minute, sp("a"), "pulse", 10, 6, 1),    // 11:30, the newest bucket
		b(5*time.Hour, sp("a"), "census", 2, 2, 0),       // 07:00
		b(5*time.Hour, sp("b"), "incident", 3, 1, 2),     // 07:00
		b(5*time.Hour, nil, "incident", 1, 0, 1),         // no product
		b(36*time.Hour, sp("b"), "pulse", 4, 0, 0),       // yesterday 00:00
		b(60*time.Hour, sp("a"), "incident", 5, 0, 5),    // two days back 00:00
		b(31*24*time.Hour, sp("a"), "pulse", 99, 99, 99), // outside every range
		b(-30*time.Minute, sp("a"), "pulse", 99, 99, 99), // after End
		b(5*time.Hour, sp("x"), "audit", 7, 0, 0),        // a provider without a network
	}}
	for i := 0; i < 30; i++ { // a's 30 newest signals, one a minute from 11:59 back
		stats.Recent = append(stats.Recent, store.RecentSignal{Provider: "a", At: at(time.Duration(i+1) * time.Minute), ID: int64(100 - i), Signal: int16(i%3 - 1)})
	}
	stats.Recent = append(stats.Recent, store.RecentSignal{Provider: "b", At: at(36 * time.Hour), ID: 5, Signal: 1},
		store.RecentSignal{Provider: "b", At: at(5 * time.Hour), ID: 7, Signal: -1})
	w := &EventWindow{Stats: stats, text: eventText{slugs: map[string]string{"a": "alpha", "b": "beta"}}}
	totals := func(req, ok float64) map[string]*RangeTotals {
		out := map[string]*RangeTotals{}
		for _, r := range []string{"24h", "7d", "30d"} {
			out[r] = &RangeTotals{Requests: Num(req), Successes: Num(ok), SuccessRate: Num(ok / req)}
		}
		return out
	}
	pg := &Pages{Networks: NetworksAnswer{Networks: []NetworkRow{{Slug: "gamma", Name: "Gamma", Kinds: []string{"residential"}},
		{Slug: "beta", Name: "Beta", Kinds: []string{"residential"}}, {Slug: "alpha", Name: "Alpha", Kinds: []string{"residential", "mobile"}}}},
		Network: map[string]*NetworkAnswer{"all/all": {Ranges: totals(1000, 900)}, "alpha/residential": {Ranges: totals(300, 240)},
			"alpha/mobile": {Ranges: totals(100, 60)}, "beta/residential": {Ranges: totals(600, 600)}}}
	return &Context{GeneratedAt: "now", Pages: pg, Events: w}
}

func statsBody(t *testing.T, bodies map[string]any, path string) *EventsStatsAnswer {
	t.Helper()
	a, ok := bodies[path].(*EventsStatsAnswer)
	if !ok {
		t.Fatalf("no body at %s", path)
	}
	return a
}

// Every field of the 24h, 7d and 30d answers over all networks.
func TestEventsStatsEveryField(t *testing.T) {
	t.Parallel()
	bodies, err := buildEventsStats(statsFixture())
	if err != nil || len(bodies) != 9 {
		t.Fatalf("%d bodies, %v", len(bodies), err)
	}
	day := statsBody(t, bodies, "/v1/events/stats/24h")
	if day.Range != "24h" || day.Network != nil || day.Start != "2026-11-08T12:00:00.000000+00:00" || day.End != "2026-11-09T12:00:00.000000+00:00" {
		t.Errorf("head %+v", day)
	}
	want := StatsTotals{Events: 23, ByCategory: CategoryCounts{Census: 2, Pulse: 10, Incident: 4, Audit: 7}, Good: 9, Bad: 4, Reporting: 2, Tracked: 2}
	if day.Totals != want {
		t.Errorf("24h totals %+v, want %+v", day.Totals, want)
	}
	if day.Pass != (StatsPass{Requests: 1000, Successes: 900, Rate: 0.9}) {
		t.Errorf("pass %+v", day.Pass)
	}
	if len(day.Buckets) != 24 || len(day.Heatmap) != 48 || day.Buckets[1].Start != "2026-11-08T13:00:00.000000+00:00" || day.Heatmap[1].Start != "2026-11-08T12:30:00.000000+00:00" {
		t.Fatalf("%d buckets, %d cells", len(day.Buckets), len(day.Heatmap))
	}
	seven := day.Buckets[19] // 07:00 to 08:00
	if seven.Total != 13 || seven.ByCategory != (CategoryCounts{Census: 2, Incident: 4, Audit: 7}) || seven.Networks != 2 {
		t.Errorf("07:00 bucket %+v", seven)
	}
	if last := day.Buckets[23]; last.Total != 10 || last.Networks != 1 || day.Heatmap[47].ByCategory.Pulse != 10 || day.Heatmap[46].ByCategory.Pulse != 0 {
		t.Errorf("newest bucket %+v, cells %+v %+v", last, day.Heatmap[46], day.Heatmap[47])
	}
	alpha, beta := day.Networks[0], day.Networks[1]
	if len(day.Networks) != 2 || alpha.Slug != "alpha" || alpha.Name != "Alpha" || alpha.Events != 12 || alpha.Good != 8 || alpha.Bad != 1 ||
		*alpha.LastAt != "2026-11-09T11:40:00.000000+00:00" || len(alpha.Recent) != 24 {
		t.Fatalf("alpha %+v", alpha)
	}
	// The 24 newest of a's 30, oldest first: the newest (11:59) is signal -1, at i=0.
	if alpha.Recent[23] != -1 || alpha.Recent[22] != 0 || alpha.Recent[0] != int16(23%3-1) {
		t.Errorf("alpha recent %v", alpha.Recent)
	}
	if beta.Slug != "beta" || beta.Events != 3 || beta.Bad != 2 || !reflect.DeepEqual(beta.Recent, []int16{-1}) {
		t.Errorf("beta %+v", beta)
	}
	month := statsBody(t, bodies, "/v1/events/stats/30d")
	if month.Totals.Events != 32 || len(month.Buckets) != 30 || len(month.Heatmap) != 60 || month.Buckets[27].Total != 5 || month.Buckets[28].Total != 4 {
		t.Errorf("30d %+v", month.Totals)
	}
	if week := statsBody(t, bodies, "/v1/events/stats/7d"); len(week.Buckets) != 28 || len(week.Heatmap) != 56 || week.Totals.Events != 32 ||
		week.Networks[0].Slug != "alpha" || week.Networks[0].Events != 17 || !reflect.DeepEqual(week.Networks[1].Recent, []int16{1, -1}) {
		t.Errorf("7d %+v %+v", week.Totals, week.Networks)
	}
}

// network= counts only that network's rows; its pass adds up its kinds.
func TestEventsStatsOfOneNetwork(t *testing.T) {
	t.Parallel()
	bodies, _ := buildEventsStats(statsFixture())
	a := statsBody(t, bodies, "/v1/events/stats/30d/alpha")
	want := StatsTotals{Events: 17, ByCategory: CategoryCounts{Census: 2, Pulse: 10, Incident: 5}, Good: 8, Bad: 6, Reporting: 1, Tracked: 1}
	if *a.Network != "alpha" || a.Totals != want || len(a.Networks) != 1 || a.Networks[0].Events != 17 {
		t.Errorf("alpha %+v", a.Totals)
	}
	if a.Pass != (StatsPass{Requests: 400, Successes: 300, Rate: 0.75}) {
		t.Errorf("pass %+v", a.Pass)
	}
	if a.Buckets[29].Networks != 1 || a.Buckets[28].Networks != 0 {
		t.Errorf("buckets %+v", a.Buckets[28:])
	}
	if _, ok := bodies["/v1/events/stats/30d/gamma"]; ok {
		t.Error("gamma has no framework provider, so no answer")
	}
}

// A range without events: zero counts, every bucket and cell still there,
// no last event and no signals; a database before v13 reads the same.
func TestEventsStatsOfAnEmptyRange(t *testing.T) {
	t.Parallel()
	c := statsFixture()
	c.Events.Stats.End = statsEnd.Add(30 * 24 * time.Hour) // a month later
	bodies, _ := buildEventsStats(c)
	a := statsBody(t, bodies, "/v1/events/stats/24h/beta")
	if a.Totals != (StatsTotals{Tracked: 1}) || len(a.Buckets) != 24 || a.Buckets[3].Total != 0 || a.Networks[0].LastAt != nil ||
		a.Networks[0].Recent == nil || len(a.Networks[0].Recent) != 0 {
		t.Errorf("empty range %+v %+v", a.Totals, a.Networks)
	}
	c.Events.Stats, c.Pages = store.EventStats{Missing: true}, nil
	bodies, _ = buildEventsStats(c)
	if a := statsBody(t, bodies, "/v1/events/stats/7d"); a.Totals != (StatsTotals{}) || a.Pass.Requests.OK() || len(a.Networks) != 0 || len(bodies) != 3 {
		t.Errorf("before v13 %+v", a)
	}
}

func TestEventsStatsKey(t *testing.T) {
	t.Parallel()
	key := func(target string) (string, error) {
		return eventsStatsKey(httptest.NewRequest(http.MethodGet, target, nil))
	}
	for target, want := range map[string]string{"/v1/events/stats": "/v1/events/stats/30d", "/v1/events/stats?range=24h&network=alpha": "/v1/events/stats/24h/alpha"} {
		if got, err := key(target); got != want || err != nil {
			t.Errorf("%s: %q %v", target, got, err)
		}
	}
	for target, want := range map[string]string{"/v1/events/stats?range=1y": "range must be", "/v1/events/stats?kind=x": "unknown query parameter",
		"/v1/events/stats?network=Bad!": "invalid network"} {
		var bad BadRequest
		if _, err := key(target); !errors.As(err, &bad) || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: %v, want %q", target, err, want)
		}
	}
}

// The registered answer is built with the build's events and again from each
// refresh's (WithEvents), never from Pages; an Events answer needs Cut and a
// Root names its paths.
func TestEventsStatsFollowTheEventsRefresh(t *testing.T) {
	t.Parallel()
	c := statsFixture()
	res := &Result{Pages: c.Pages}
	res.Events = *c.Events
	c.Events = &res.Events
	res.Events.buildLive(c)
	before := statsBody(t, res.Events.Held["events-stats"], "/v1/events/stats/24h")
	stats := res.Events.Stats
	stats.Buckets = append(stats.Buckets[:len(stats.Buckets):len(stats.Buckets)], store.EventBucket{Start: statsEnd.Add(-time.Hour), Provider: sp("b"), Category: "pulse", Events: 5, LastAt: statsEnd})
	next := WithEvents(res, &store.Inputs{EventStats: stats})
	after := statsBody(t, next.Events.Held["events-stats"], "/v1/events/stats/24h")
	if before.Totals.Events != 23 || after.Totals.Events != 28 || next.Events.live.Events != &next.Events || len(next.Events.Unbuilt) != 0 {
		t.Errorf("before %d, after %d", before.Totals.Events, after.Totals.Events)
	}
	a := answers["events-stats"]
	if !a.Owns("/v1/events/stats/24h") || a.Owns("/v1/events") || a.Owns("/v1/events-stats") {
		t.Error("Owns does not follow Root")
	}
	for _, bad := range []PageAnswer{{Name: "x1", Events: true}, {Name: "x2", Root: "/x", Cut: a.Cut}} {
		bad.Build, bad.Key, bad.Route = a.Build, a.Key, "GET /v1/x"
		func() {
			defer func() {
				if recover() == nil {
					t.Errorf("%s registered", bad.Name)
				}
			}()
			RegisterAnswer(bad)
		}()
	}
}
