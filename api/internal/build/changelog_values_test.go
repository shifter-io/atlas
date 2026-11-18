package build

import (
	"encoding/json"
	"reflect"
	"testing"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
)

// changeByID is the fixture's 30d entries (or rangeID's) by id.
func changeByID(t *testing.T, rangeID string) map[string]ChangeEntry {
	t.Helper()
	c := changeFixture()
	if err := c.Pages.addChangelog(c); err != nil {
		t.Fatal(err)
	}
	out := map[string]ChangeEntry{}
	for _, en := range c.Pages.Changelog[rangeID].Entries {
		out[en.ID] = en
	}
	return out
}

// jsonOf is v as the API writes it, decoded (numbers as float64).
func jsonOf(t *testing.T, v any) any {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var out any
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

// One case per kind: each entry's values carry only its kind's keys, the
// figures its title and delta are made from, and a null only with its
// reason.
func TestEveryChangeKindHasItsValues(t *testing.T) {
	t.Parallel()
	month, week := changeByID(t, "30d"), changeByID(t, "7d")
	cases := []struct {
		name, event string
		en          ChangeEntry
		values      string
		naReasons   string
	}{
		{"pool", "pool_grew", month["chg-pool-alpha"], `{"uniqueIps":130,"previousUniqueIps":100,"deltaPct":30}`, `null`},
		{"price", "price_cut", month["chg-price-alpha-residential-2026-11-05"], `{"fromRate":2,"toRate":1.5,"deltaPct":-25,"plan":null}`,
			`{"values.plan":"not_published"}`},
		{"success", "success_fell", month["chg-success-alpha"], `{"metric":"success","value":0.9375,"previous":0.97,"delta":-3.3,"basis":null}`,
			`{"values.basis":"not_applicable"}`},
		{"latency", "latency_rose", week["chg-latency-alpha"], `{"metric":"latency","value":841,"previous":700,"delta":20,"basis":"week"}`, `null`},
		{"coverage", "countries_lost", month["chg-coverage-alpha"],
			`{"countries":1,"previousCountries":2,"delta":-1,"lostCountryCodes":["PL"],"comparedRange":"7d"}`, `null`},
		{"authenticity", "geo_accuracy_fell", month["chg-authenticity-alpha"], `{"value":0.9,"previous":0.95,"delta":-5}`, `null`},
		{"methodology", "catalog", month["chg-cat-1"], `{"fromId":null,"toId":null}`, `{"values.fromId":"catalog","values.toId":"catalog"}`},
		{"catalog price", "catalog", month["chg-cat-2"], `{"fromRate":null,"toRate":null,"deltaPct":null,"plan":null}`,
			`{"values.fromRate":"catalog","values.toRate":"catalog","values.deltaPct":"catalog","values.plan":"not_published"}`},
		{"incident", "incident", month["chg-outage-alpha-res-2026-11-10T10:00:00Z"], `{}`, `null`},
	}
	for _, c := range cases {
		var values, reasons any
		if err := json.Unmarshal([]byte(c.values), &values); err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal([]byte(c.naReasons), &reasons); err != nil {
			t.Fatal(err)
		}
		if c.en.ID == "" {
			t.Errorf("%s: no entry", c.name)
			continue
		}
		if c.en.Event != c.event {
			t.Errorf("%s: event %q, want %q", c.name, c.en.Event, c.event)
		}
		if got := jsonOf(t, c.en.Values); !reflect.DeepEqual(got, values) {
			t.Errorf("%s values %v, want %v", c.name, got, values)
		}
		if got := jsonOf(t, c.en.NAReasons); !reflect.DeepEqual(got, reasons) {
			t.Errorf("%s naReasons %v, want %v", c.name, got, reasons)
		}
	}
}

// The other direction of every move (the fixture's are falls, a latency
// rise and a pool rise), a coverage loss against 24h with a country twice
// in the longer window's breakdown (its code once), and a price raise: every
// change_event code is made, each made one is registered, and each move's
// severity says whether it is for the better.
func TestEveryChangeEventIsMadeAndRegistered(t *testing.T) {
	t.Parallel()
	base := func(kind, id string, _ float64) ChangeEntry { return ChangeEntry{ID: id, Kind: kind, Severity: "info"} }
	ws := &WindowStats{SuccessDeltaPts: 3, LatencyDeltaPct: -20, successEdges: trendEdges{0.9, 0.93}, latencyEdges: trendEdges{1000, 800},
		LatencyDeltaBasis: model.S(DeltaBasisDay), UniqueIps: nan}
	byEvent := map[string]ChangeEntry{}
	add := func(entries []ChangeEntry) {
		for _, en := range entries {
			if _, ok := byEvent[en.Event]; !ok {
				byEvent[en.Event] = en
			}
		}
	}
	add(performanceEntries(base, ws))
	add(poolEntries(base, dailyPool([2]Num{100, 90}, [2]Num{100, 100}, [2]Num{100, 90}, [2]Num{100, 80}, [2]Num{100, 70}, [2]Num{100, 60}), "30d"))
	de, pl := snap.CountryIPs{Country: "Germany", CountryCode: "DE", Ips: 10}, snap.CountryIPs{Country: "Poland", CountryCode: "PL", Ips: 5}
	add(recentEntries(base, &snap.Product{Windows: map[string]snap.Window{"24h": changeWindow(50, 0.95, de), "7d": changeWindow(300, 0.9, de, pl, pl)}}, "24h"))
	c := changeFixture()
	c.Inputs.PricingHistory[0].PricePerGb, c.Inputs.PricingHistory[1].PricePerGb = fp(1.5), fp(2)
	add(priceEntries(c, 30*86400))
	add(changeEntries(changeFixture(), Ranges[2]))
	add(changeEntries(changeFixture(), Ranges[1]))
	for _, code := range codeSets["change_event"].Values {
		if _, ok := byEvent[code.Code]; !ok {
			t.Errorf("no entry with event %s", code.Code)
		}
	}
	registered := map[string]bool{}
	for _, code := range codeSets["change_event"].Values {
		registered[code.Code] = true
	}
	for ev := range byEvent {
		if !registered[ev] {
			t.Errorf("event %q is not registered", ev)
		}
	}
	want := map[string]string{
		"success_rose":      `{"metric":"success","value":0.93,"previous":0.9,"delta":3,"basis":null}`,
		"latency_fell":      `{"metric":"latency","value":800,"previous":1000,"delta":-20,"basis":"day"}`,
		"pool_shrank":       `{"uniqueIps":70,"previousUniqueIps":100,"deltaPct":-30}`,
		"geo_accuracy_rose": `{"value":0.95,"previous":0.9,"delta":5}`,
		"countries_lost":    `{"countries":1,"previousCountries":2,"delta":-1,"lostCountryCodes":["PL"],"comparedRange":"24h"}`,
		"price_raised":      `{"fromRate":1.5,"toRate":2,"deltaPct":33.3,"plan":null}`,
	}
	for ev, w := range want {
		var values any
		if err := json.Unmarshal([]byte(w), &values); err != nil {
			t.Fatal(err)
		}
		if got := jsonOf(t, byEvent[ev].Values); !reflect.DeepEqual(got, values) {
			t.Errorf("%s values %v, want %v", ev, got, values)
		}
	}
	severity := map[string]string{"success_rose": "good", "latency_fell": "good", "pool_shrank": "bad", "geo_accuracy_rose": "good",
		"countries_lost": "bad", "price_raised": "bad"}
	for ev, w := range severity {
		if got := byEvent[ev].Severity; got != w {
			t.Errorf("%s severity %q, want %q", ev, got, w)
		}
	}
}

// An ongoing and a closed incident over several countries, a census one,
// and the profile's incidents[] of the same outages: the same facts.
func TestIncidentFactsOverSeveralCountries(t *testing.T) {
	t.Parallel()
	month := changeByID(t, "30d")
	ongoing := month["chg-outage-alpha-res-2026-11-10T11:00:00Z"].Incident
	want := model.IncidentFacts{Countries: 2, CountryCodes: []string{"FR", "US"}, Kind: "down", DetectedBy: "pulse", Ongoing: true,
		StartedAt: model.S("2026-11-10T11:00:00.000Z")}
	if ongoing == nil || !reflect.DeepEqual(ongoing.IncidentFacts, want) {
		t.Errorf("ongoing %+v, want %+v", ongoing, want)
	}
	if old := month["chg-outage-alpha-res-2026-10-25T00:00:00Z"].Incident; old == nil || old.Kind != "census_stall" || old.DetectedBy != "census" ||
		model.SV(old.EndedAt) != "2026-10-25T00:45:00.000Z" || old.Ongoing {
		t.Errorf("census incident %+v", old)
	}
	closed := []snap.Outage{
		{Country: "PL", Source: "census", StartedAt: "2026-11-10T09:10:00Z", EndedAt: endedAt("2026-11-10T09:50:00Z")},
		{Country: "DE", Source: "pulse", StartedAt: "2026-11-10T09:00:00+00:00", EndedAt: endedAt("bad"), DurationS: 1800},
		{Country: "PL", Source: "pulse", StartedAt: "2026-11-10T09:20:00Z", EndedAt: endedAt("2026-11-10T09:40:00Z")}}
	want = model.IncidentFacts{Countries: 2, CountryCodes: []string{"DE", "PL"}, Kind: "down", DetectedBy: "pulse",
		StartedAt: model.S("2026-11-10T09:00:00.000Z"), EndedAt: model.S("2026-11-10T09:50:00.000Z")}
	if got := incidentFacts(closed); !reflect.DeepEqual(got, want) {
		t.Errorf("closed %+v, want %+v", got, want)
	}
	if got := incidentFacts([]snap.Outage{{Country: "DE", StartedAt: "bad"}}); got.StartedAt != nil || got.EndedAt != nil || !got.Ongoing || got.Countries != 1 {
		t.Errorf("unreadable start %+v", got)
	}
	sp := changeFixture().Entries[0].Snap["residential"]
	incidents := outageIncidents(sp, snap.Ms(changeAsOf), 40)
	if closedDE := month["chg-outage-alpha-res-2026-11-10T10:00:00Z"].Incident; len(incidents) != 4 || closedDE == nil ||
		!reflect.DeepEqual(incidents[0].IncidentFacts, closedDE.IncidentFacts) {
		t.Errorf("profile incident %+v, changelog %+v", incidents, closedDE)
	}
	if us := incidents[1]; us.CountryCodes[0] != "US" || !us.Ongoing || us.EndedAt != nil || us.Kind != "down" {
		t.Errorf("profile ongoing incident %+v", us)
	}
}
