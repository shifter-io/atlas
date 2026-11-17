package build

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

const changeAsOf = "2026-11-10T12:00:00Z"

func changeWindow(completed Num, geo Num, countries ...snap.CountryIPs) snap.Window {
	w := snap.EmptyWindow()
	w.Period.End = changeAsOf
	w.Metrics.Completed = completed
	w.Pool.GeoAccuracy = geo
	w.Pool.CountriesBreakdown = countries
	return w
}

func endedAt(s string) *string { return &s }

// dailyPool is a 30d window ending at changeAsOf whose daily series rows
// (24 hours apart, the last ending at changeAsOf) have these requests and
// unique IPs, oldest first.
func dailyPool(rows ...[2]Num) snap.Window {
	w := changeWindow(1000, nan)
	end, _ := snap.ParseTime(changeAsOf)
	for i, r := range rows {
		at := end.Add(time.Duration(i-len(rows)) * 24 * time.Hour)
		ips := r[1]
		if r[0] == 0 {
			ips = nan
		}
		w.Series = append(w.Series, snap.SeriesRow{At: at.Format(time.RFC3339), Completed: r[0], UniqueIps: ips})
	}
	return w
}

// changeFixture is a Context with every kind of entry: alpha has a closed
// and an ongoing incident today (the ongoing one over two countries) and an
// older one, a success fall, a pool rise, a lost country, a geo accuracy
// fall, a price cut and a catalog row, and moved up two ranks in 30 days
// and one in 7 (its snapshot's previous rank, the build before, is its
// rank); beta is not measured; gamma moved down with no entry of its own;
// one catalog row is fleet-wide.
func changeFixture() *Context {
	de, pl := snap.CountryIPs{Country: "Germany", CountryCode: "DE", Ips: 10}, snap.CountryIPs{Country: "Poland", CountryCode: "PL", Ips: 5}
	day := changeWindow(50, nan, de)
	day.Series = []snap.SeriesRow{{At: "2026-11-10T10:00:00Z", SuccessRate: 0.8}, {At: "2026-11-10T10:05:00Z", SuccessRate: 0.6},
		{At: "2026-11-10T10:20:00Z", SuccessRate: 0.1}}
	week := changeWindow(300, 0.90, de)
	week.Series = []snap.SeriesRow{{At: "2026-11-10T00:00:00Z", SuccessRate: 0.7}}
	// The month's pool: no requests, then the day measurement started in
	// (5 IPs), three counted days (100, 100, 130) and the day ending at the
	// cutoff (500), which two counted days alone compare.
	month := dailyPool([2]Num{0, 0}, [2]Num{100, 5}, [2]Num{100, 100}, [2]Num{100, 100}, [2]Num{100, 130}, [2]Num{100, 500})
	month.Pool.GeoAccuracy, month.Pool.CountriesBreakdown = 0.95, []snap.CountryIPs{de, pl}
	month.Uptime = &snap.Uptime{Countries: 40}
	sp := &snap.Product{ID: "alpha-res", Windows: map[string]snap.Window{"24h": day, "7d": week, "30d": month}, Outages: []snap.Outage{
		{Country: "DE", Source: "pulse", StartedAt: "2026-11-10T10:00:00Z", EndedAt: endedAt("2026-11-10T10:20:00Z"), DurationS: 1200},
		{Country: "US", Source: "pulse", StartedAt: "2026-11-10T11:00:00Z"},
		{Country: "FR", Source: "pulse", StartedAt: "2026-11-10T11:10:00Z", EndedAt: endedAt("2026-11-10T11:30:00Z"), DurationS: 1200},
		{Country: "DE", Source: "census", StartedAt: "2026-10-25T00:00:00Z", EndedAt: endedAt("2026-10-25T00:45:00Z"), DurationS: 2700}}}
	provider := func(slug, name string, rank, previous Num) *model.Provider {
		return &model.Provider{Slug: slug, Name: name, Source: model.SourceMeasured, PrimaryKind: "residential",
			Score: model.ScoreView{Rank: rank, PreviousRank: previous}}
	}
	// The 7d moves compare up to the UTC day before (latency) and the slot
	// ending at 11:30 (success); the 30d success change has no time.
	stats := map[string]map[string]*WindowStats{"alpha": {
		"24h": {SuccessRate: 0.99, SuccessDeltaPts: 1, LatencyDeltaPct: nan, UniqueIps: 100},
		"7d": {SuccessRate: 0.97, SuccessDeltaPts: 2.5, LatencyDeltaPct: 20, LatencyP50: 812.4, UniqueIps: 900,
			successEdges: trendEdges{0.955, 0.98}, latencyEdges: trendEdges{700.4, 840.6}, LatencyDeltaBasis: model.S(DeltaBasisWeek),
			successAt: snap.Ms("2026-11-10T11:30:00Z"), latencyAt: snap.Ms("2026-11-10T00:00:00Z")},
		"30d": {SuccessRate: 0.95, SuccessDeltaPts: -3.25, LatencyDeltaPct: -5, UniqueIps: 4321,
			successEdges: trendEdges{0.97, 0.9375}}}}
	generated, _ := snap.ParseTime(changeAsOf)
	today := utcDay(generated)
	// The kept builds' ranks: alpha was 4th 20 days ago and 3rd 3 days ago,
	// gamma 1st until it was 2nd 12 hours ago (its rank of 40 days ago is
	// outside every range).
	ago := func(hours int) time.Time { return generated.Add(-time.Duration(hours) * time.Hour) }
	ranks := []store.RankRow{{AsOf: ago(40 * 24), Network: "gamma-res", Rank: 9}, {AsOf: ago(20 * 24), Network: "alpha-res", Rank: 4},
		{AsOf: ago(20 * 24), Network: "gamma-res", Rank: 1}, {AsOf: ago(3 * 24), Network: "alpha-res", Rank: 3},
		{AsOf: ago(3 * 24), Network: "gamma-res", Rank: 1}, {AsOf: ago(12), Network: "alpha-res", Rank: 2}, {AsOf: ago(12), Network: "gamma-res", Rank: 2}}
	gamma := &snap.Product{ID: "gamma-res", Windows: map[string]snap.Window{}}
	return &Context{GeneratedAt: changeAsOf, Stats: stats, Pages: &Pages{},
		Entries: []*entry{{P: provider("alpha", "Alpha Net", 2, 2), Snap: map[string]*snap.Product{"residential": sp}},
			{P: provider("beta", "Beta", 3, 3)}, {P: provider("gamma", "Gamma", 3, 3), Snap: map[string]*snap.Product{"residential": gamma}}},
		Inputs: &store.Inputs{Ranks: ranks, PricingHistory: []store.PricingHistoryRow{
			{ProviderSlug: "alpha", Category: "residential", EffectiveFrom: today.AddDate(0, 0, -60), PricePerGb: fp(2)},
			{ProviderSlug: "alpha", Category: "residential", EffectiveFrom: today.AddDate(0, 0, -5), PricePerGb: fp(1.5)}}},
		Dataset: &model.Dataset{RunID: model.S("preview-1"), Changelog: []model.ChangeEvent{
			{ID: "chg-cat-1", Kind: "methodology", Delta: nan, Severity: "info", AgoSeconds: 3600, Source: model.SourceMeasured},
			{ID: "chg-cat-2", Kind: "pricing", ProviderSlug: model.S("alpha"), ProviderName: model.S("Alpha Net"),
				Delta: nan, Severity: "good", AgoSeconds: 2 * 86400, Source: model.SourceMeasured}}}}
}

func changeBodies(t *testing.T, c *Context) map[string][]byte {
	t.Helper()
	if err := c.Pages.addChangelog(c); err != nil {
		t.Fatal(err)
	}
	bodies, err := buildChangelog(c)
	if err != nil {
		t.Fatal(err)
	}
	out := map[string][]byte{}
	for path, body := range bodies {
		b, err := json.MarshalIndent(body, "", " ")
		if err != nil {
			t.Fatal(err)
		}
		out[path] = b
	}
	return out
}

// The fixture's three bodies are testdata/changelog.golden.json
// (SITEAPI_UPDATE_GOLDENS=1 rewrites it).
func TestTheChangelogGolden(t *testing.T) {
	t.Parallel()
	bodies := changeBodies(t, changeFixture())
	var file bytes.Buffer
	for _, path := range slices.Sorted(maps.Keys(bodies)) {
		fmt.Fprintf(&file, "%s\n%s\n", path, bodies[path])
	}
	name := filepath.Join("testdata", "changelog.golden.json")
	if os.Getenv("SITEAPI_UPDATE_GOLDENS") != "" {
		if err := os.WriteFile(name, file.Bytes(), 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(name)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(want, file.Bytes()) {
		t.Errorf("changelog bodies differ from %s:\n%s", name, file.Bytes())
	}
}

// What each range holds, and why: the figures behind the golden.
func TestTheChangelogEntries(t *testing.T) {
	t.Parallel()
	c := changeFixture()
	if err := c.Pages.addChangelog(c); err != nil {
		t.Fatal(err)
	}
	ids := func(rangeID string) string {
		var out []string
		for _, en := range c.Pages.Changelog[rangeID].Entries {
			out = append(out, en.ID)
		}
		return strings.Join(out, " ")
	}
	ongoing, closed, old := "chg-outage-alpha-res-2026-11-10T11:00:00Z", "chg-outage-alpha-res-2026-11-10T10:00:00Z", "chg-outage-alpha-res-2026-10-25T00:00:00Z"
	want := map[string]string{
		"24h": "chg-cat-1 " + ongoing + " " + closed,
		"7d": "chg-authenticity-alpha chg-coverage-alpha chg-success-alpha chg-cat-1 " + ongoing + " " + closed +
			" chg-latency-alpha chg-cat-2 chg-price-alpha-residential-2026-11-05",
		"30d": "chg-authenticity-alpha chg-coverage-alpha chg-success-alpha chg-cat-1 " + ongoing + " " + closed +
			" chg-pool-alpha chg-cat-2 chg-price-alpha-residential-2026-11-05 " + old,
	}
	for rangeID, w := range want {
		if got := ids(rangeID); got != w {
			t.Errorf("%s: %s\nwant %s", rangeID, got, w)
		}
	}
	byID := map[string]ChangeEntry{}
	for _, en := range c.Pages.Changelog["30d"].Entries {
		byID[en.ID] = en
	}
	if i := byID[ongoing].Incident; i == nil || i.Severity != "moderate" || len(i.CountryCodes) != 2 ||
		i.DurationMinutes.OK() || i.Impact != 0.05 || byID[ongoing].Severity != "bad" {
		t.Errorf("ongoing incident: %+v %+v", byID[ongoing], i)
	}
	if i := byID[closed].Incident; i == nil || i.Severity != "minor" || !slices.Equal(i.CountryCodes, []string{"DE"}) ||
		i.DurationMinutes != 20 || i.Impact != 0.025 || byID[closed].Severity != "info" || byID[closed].AgoSeconds != 7200 {
		t.Errorf("closed incident: %+v %+v", byID[closed], i)
	}
	if i := byID[old].Incident; i == nil || i.Severity != "moderate" || i.DurationMinutes != 45 {
		t.Errorf("old incident: %+v", i)
	}
	// The pool compares two counted days, the newer ending a day before the
	// build (CHANGELOG-REAL-1, #1314).
	if p := byID["chg-pool-alpha"]; p.Values["deltaPct"] != Num(30) || p.Severity != "good" || p.Event != "pool_grew" || p.AgoSeconds != 86400 {
		t.Errorf("pool: %+v", p)
	}
	if p := byID["chg-price-alpha-residential-2026-11-05"]; p.Kind != "price" || p.Values["deltaPct"] != Num(-25) || p.Severity != "good" ||
		p.Event != "price_cut" || p.Values["fromRate"] != Num(2) || p.Values["toRate"] != Num(1.5) || p.AgoSeconds != 5*86400+12*3600 {
		t.Errorf("price: %+v", p)
	}
	if s := byID["chg-success-alpha"]; s.Values["delta"] != Num(-3.3) || s.Event != "success_fell" || s.Severity != "bad" || s.Network.Name != "Alpha Net" {
		t.Errorf("success: %+v", s)
	}
	// A catalog row serves its kind, network and severity: neither its
	// English headline and detail (#962) nor its delta and unit (#990).
	if cat := byID["chg-cat-2"]; cat.Kind != "price" || cat.Event != "catalog" || cat.Network == nil || cat.Network.Name != "Alpha Net" ||
		byID["chg-cat-1"].Network != nil {
		t.Errorf("catalog: %+v %+v", cat, byID["chg-cat-1"])
	}
	for _, en := range c.Pages.Changelog["30d"].Entries {
		b, err := json.Marshal(en)
		if err != nil {
			t.Fatal(err)
		}
		var fields map[string]any
		if err := json.Unmarshal(b, &fields); err != nil {
			t.Fatal(err)
		}
		for _, gone := range []string{"title", "detail", "delta", "unit"} {
			if _, ok := fields[gone]; ok {
				t.Errorf("%s serves %s", en.ID, gone)
			}
		}
	}
	movers := c.Pages.Changelog["30d"].Movers
	if len(movers) != 2 || movers[0].Slug != "alpha" || movers[0].Delta != 2 || model.SV(movers[0].ReasonID) != "chg-authenticity-alpha" ||
		movers[1].Slug != "gamma" || movers[1].Delta != -2 || movers[1].ReasonID != nil {
		t.Errorf("movers: %+v", movers)
	}
	// Each range's movers compare the rank with the range's oldest kept one.
	if m := c.Pages.Changelog["7d"].Movers; len(m) != 2 || m[0].Slug != "gamma" || m[0].PreviousRank != 1 || m[0].Delta != -2 ||
		m[1].Slug != "alpha" || m[1].PreviousRank != 3 || m[1].Delta != 1 {
		t.Errorf("7d movers: %+v", m)
	}
	if m := c.Pages.Changelog["24h"].Movers; len(m) != 1 || m[0].Slug != "gamma" || m[0].Delta != -1 || m[0].ReasonID != nil {
		t.Errorf("24h movers (alpha did not move in a day): %+v", m)
	}
}

// A build with nothing to log answers empty lists, not nulls.
func TestAnEmptyChangelog(t *testing.T) {
	t.Parallel()
	c := &Context{GeneratedAt: changeAsOf, Pages: &Pages{}, Inputs: &store.Inputs{}}
	for path, body := range changeBodies(t, c) {
		if !strings.Contains(string(body), `"entries": []`) || !strings.Contains(string(body), `"movers": []`) || !strings.Contains(string(body), `"runId": null`) {
			t.Errorf("%s: %s", path, body)
		}
	}
	if _, err := buildChangelog(&Context{Pages: &Pages{}}); err == nil {
		t.Error("a build without records answered")
	}
}

// longChangelog adds n catalog rows of alpha a day old and older.
func longChangelog(c *Context, n int) {
	for i := range n {
		c.Dataset.Changelog = append(c.Dataset.Changelog, model.ChangeEvent{ID: fmt.Sprintf("chg-long-%04d", i), Kind: "pool",
			ProviderSlug: model.S("alpha"), ProviderName: model.S("Alpha Net"), Delta: 1.5, Unit: "%",
			Severity: "good", AgoSeconds: Num(86400 + i), Source: model.SourceMeasured})
	}
}

// compactSizes is each range's body size as the server renders it.
func compactSizes(t *testing.T, c *Context) map[string]int {
	t.Helper()
	out := map[string]int{}
	for rangeID, rec := range c.Pages.Changelog {
		b, err := json.Marshal(rec)
		if err != nil {
			t.Fatal(err)
		}
		out[rangeID] = len(b)
	}
	return out
}

// A long history keeps the newest ChangelogMaxEntries within the cap; the
// network answer counts all of its network's.
func TestALongChangelogIsCutToItsNewestEntries(t *testing.T) {
	t.Parallel()
	c := changeFixture()
	longChangelog(c, 2*ChangelogMaxEntries)
	c.Pages.Network = map[string]*NetworkAnswer{"alpha/residential": {}}
	if err := c.Pages.addChangelog(c); err != nil {
		t.Fatal(err)
	}
	rec := c.Pages.Changelog["30d"]
	if len(rec.Entries) != ChangelogMaxEntries || rec.Entries[len(rec.Entries)-1].AgoSeconds > 86400+ChangelogMaxEntries {
		t.Errorf("%d entries, oldest %v", len(rec.Entries), rec.Entries[len(rec.Entries)-1].AgoSeconds)
	}
	for rangeID, n := range compactSizes(t, c) {
		if n > MaxAnswerBytes {
			t.Errorf("%s: %d bytes, above the cap", rangeID, n)
		}
	}
	ans := c.Pages.Network["alpha/residential"]
	if len(ans.Changelog) != RecordCap || ans.Records.Changelog <= ChangelogMaxEntries || ans.Changelog[0].ID != rec.Entries[0].ID {
		t.Errorf("network changelog %d of %d, first %s", len(ans.Changelog), ans.Records.Changelog, ans.Changelog[0].ID)
	}
}

// A real build: the network answer's changelog[] is the changelog's own
// entries of that network, named as the catalog names it.
func TestTheNetworkChangelogIsTheChangelogs(t *testing.T) {
	t.Parallel()
	in := pagedInputs(t)
	before, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	generated, _ := snap.ParseTime(before.Dataset.GeneratedAt)
	in.Changelog = []store.ChangeRow{{ID: "chg-net00", Kind: "pool", ProviderSlug: model.S("net00"), Headline: "{name} pool grew", Severity: "good",
		OccurredAt: generated.Add(-time.Hour)}, {ID: "chg-fleet", Kind: "methodology", Headline: "New method", Severity: "info", OccurredAt: generated}}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	rec, _ := res.Pages.Registered["/v1/changelog/30d"].(*ChangeRecord)
	if rec == nil {
		t.Fatalf("no /v1/changelog/30d: %v", res.Pages.Unbuilt)
	}
	var mine []ChangeEntry
	for _, en := range rec.Entries {
		if en.Network != nil && en.Network.Slug == "net00" {
			mine = append(mine, en)
		}
	}
	name := res.Details["net00"].Provider.Name
	for key, ans := range res.Pages.Network {
		if !strings.HasPrefix(key, "net00/") {
			continue
		}
		got, _ := json.Marshal(ans.Changelog)
		want, _ := json.Marshal(mine[:min(len(mine), RecordCap)])
		if !bytes.Equal(got, want) || ans.Records.Changelog != len(mine) || len(mine) == 0 || mine[len(mine)-1].Network.Name != name {
			t.Errorf("%s: changelog %s, want %s (%d)", key, got, want, ans.Records.Changelog)
		}
	}
}

func TestTheChangelogKey(t *testing.T) {
	t.Parallel()
	key := answers["changelog"].Key
	for target, want := range map[string]string{"/v1/changelog": "/v1/changelog/30d", "/v1/changelog?range=24h": "/v1/changelog/24h"} {
		if got, err := key(httptest.NewRequest(http.MethodGet, target, nil)); err != nil || got != want {
			t.Errorf("%s: %s %v", target, got, err)
		}
	}
	for target, want := range map[string]string{"/v1/changelog?range=1y": "invalid_parameter map[allowed:[24h 7d 30d] name:range", "/v1/changelog?kind=pool": "unknown_parameter map[name:"} {
		var bad APIError
		if _, err := key(httptest.NewRequest(http.MethodGet, target, nil)); !errors.As(err, &bad) || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: %v, want %q", target, err, want)
		}
	}
	if got := answers["changelog"].Request("/v1/changelog/7d"); got != "/v1/changelog?range=7d" {
		t.Errorf("request %s", got)
	}
}

// The severity bands and the outage merge.
func TestIncidentSeverityAndGroups(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		minutes, share float64
		want           string
	}{{29, 0.04, "minor"}, {30, 0.04, "moderate"}, {29, 0.05, "moderate"}, {120, 0.25, "moderate"}, {121, 0, "major"}, {5, 0.26, "major"}} {
		if got := incidentSeverity(c.minutes, c.share); got != c.want {
			t.Errorf("%v min, %v: %s, want %s", c.minutes, c.share, got, c.want)
		}
	}
	asOf := snap.Ms(changeAsOf)
	groups := outageGroups([]snap.Outage{{Country: "DE", StartedAt: "bad"}, {Country: "DE", StartedAt: "2026-11-10T09:00:00Z", EndedAt: endedAt("x"), DurationS: 600},
		{Country: "DE", StartedAt: "2026-11-10T09:05:00Z", EndedAt: endedAt("2026-11-10T09:20:00Z")}}, asOf)
	if len(groups) != 1 || !slices.Equal(incidentFacts(groups[0].outages).CountryCodes, []string{"DE"}) || groups[0].end != snap.Ms("2026-11-10T09:20:00Z") {
		t.Errorf("groups %+v", groups)
	}
}

// A network answer's changelog is attached after its nulls are listed: its
// entries' nulls (a performance entry's incident) join the answer's na with
// their reason, and the answer's own paths keep theirs (HEADLINE-PULSE-1).
func TestANetworkAnswersChangelogNullsAreListed(t *testing.T) {
	t.Parallel()
	a := &NetworkAnswer{Changelog: []ChangeEntry{{ID: "chg-1", Kind: "performance", Network: &ChangeNetwork{Slug: "net00"}}}}
	a.set([]string{"profile.pool.claimedPoolSize"}, []string{NACatalog})
	if err := a.listChangelog(); err != nil {
		t.Fatal(err)
	}
	if got := a.ReasonOf("changelog[].incident"); got != NANotApplicable {
		t.Errorf("changelog[].incident reason %q, want %s (na %v)", got, NANotApplicable, a.NA)
	}
	if got := a.ReasonOf("profile.pool.claimedPoolSize"); got != NACatalog {
		t.Errorf("own path reason %q, want %s", got, NACatalog)
	}
	if !slices.IsSorted(a.NA) {
		t.Errorf("na %v is not sorted", a.NA)
	}
}
