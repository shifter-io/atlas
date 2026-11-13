package build

import (
	"encoding/json"
	"reflect"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// changeByID is the fixture's 30d entries (or rangeID's) by id.
func changeByID(t *testing.T, rangeID string) map[string]ChangeEntry {
	t.Helper()
	c := changeFixture()
	c.Pages.addChangelog(c)
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
		name      string
		en        ChangeEntry
		values    string
		naReasons string
	}{
		{"pool", month["chg-pool-alpha"], `{"uniqueIps":130,"previousUniqueIps":100,"deltaPct":30}`, `null`},
		{"price", month["chg-price-alpha-residential-2026-11-05"], `{"fromRate":2,"toRate":1.5,"plan":null}`, `{"values.plan":"not_published"}`},
		{"success", month["chg-success-alpha"], `{"metric":"success","value":0.9375,"previous":0.97,"delta":-3.3}`, `null`},
		{"latency", week["chg-latency-alpha"], `{"metric":"latency","value":841,"previous":700,"delta":20}`, `null`},
		{"coverage", month["chg-coverage-alpha"], `{"countries":1,"previousCountries":2,"delta":-1}`, `null`},
		{"authenticity", month["chg-authenticity-alpha"], `{"value":0.9,"previous":0.95,"delta":-5}`, `null`},
		{"methodology", month["chg-cat-1"], `{"fromId":null,"toId":null}`, `{"values.fromId":"catalog","values.toId":"catalog"}`},
		{"catalog price", month["chg-cat-2"], `{"fromRate":null,"toRate":null,"plan":null}`,
			`{"values.fromRate":"catalog","values.toRate":"catalog","values.plan":"not_published"}`},
		{"incident", month["chg-outage-alpha-res-2026-11-10T10:00:00Z"], `{}`, `null`},
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
		if got := jsonOf(t, c.en.Values); !reflect.DeepEqual(got, values) {
			t.Errorf("%s values %v, want %v", c.name, got, values)
		}
		if got := jsonOf(t, c.en.NAReasons); !reflect.DeepEqual(got, reasons) {
			t.Errorf("%s naReasons %v, want %v", c.name, got, reasons)
		}
		if d, ok := c.en.Values["delta"].(Num); ok && d != c.en.Delta {
			t.Errorf("%s: values.delta %v, delta %v", c.name, d, c.en.Delta)
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
