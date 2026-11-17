package build

import (
	"encoding/json"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// COUNTRY-MIN-IPS-1: a network answer lists each requested country's status
// (code set country_status) over its groups: a country counts when it counts
// in one of them, with the most unique IPs of those; a group without the
// framework's country_status adds nothing.
func TestRequestedCountryStatusMergesTheGroups(t *testing.T) {
	var groups []*snap.Product
	for _, criteria := range []string{
		`{"country":null,"requested_countries":["DE","FR","IR"],"groups":2,"country_status":[` +
			`{"country":"FR","status":"too_few_ips","unique_ips":6},` +
			`{"country":"IR","status":"no_daily_test","unique_ips":null},` +
			`{"country":"DE","status":"counts","unique_ips":40}]}`,
		`{"country":null,"requested_countries":["FR","DE"],"groups":1,"country_status":[` +
			`{"country":"FR","status":"counts","unique_ips":12},` +
			`{"country":"DE","status":"counts","unique_ips":100}]}`,
		`{"country":null,"requested_countries":["US"],"groups":1}`,
	} {
		p := &snap.Product{}
		if err := json.Unmarshal([]byte(criteria), &p.Criteria); err != nil {
			t.Fatal(err)
		}
		groups = append(groups, p)
	}
	body, err := json.Marshal(requestedCountryStatus(groups))
	if err != nil {
		t.Fatal(err)
	}
	want := `[{"country":"DE","status":"counts","uniqueIps":100},` +
		`{"country":"FR","status":"counts","uniqueIps":12},` +
		`{"country":"IR","status":"no_daily_test","uniqueIps":null}]`
	if string(body) != want {
		t.Fatalf("got %s, want %s", body, want)
	}
	set := map[string]bool{}
	for _, c := range codeSets["country_status"].Values {
		set[c.Code] = true
	}
	for _, row := range requestedCountryStatus(groups) {
		if !set[row.Status] {
			t.Errorf("status %q is not in code set country_status", row.Status)
		}
	}
	if got := requestedCountryStatus(groups[2:]); len(got) != 0 {
		t.Errorf("a build without country_status: %v, want none", got)
	}
}

// LOWAVAIL-1: no_exits ("no exits", no IP found) ranks after too_few_ips ("low
// availability") and before no_daily_test: a group that found a few IPs shows
// low availability, one that found none shows no exits.
func TestRequestedCountryStatusRanksNoExits(t *testing.T) {
	var groups []*snap.Product
	for _, criteria := range []string{
		`{"country":null,"requested_countries":["NL","TV","WF"],"groups":2,"country_status":[` +
			`{"country":"NL","status":"no_exits","unique_ips":0},` +
			`{"country":"TV","status":"no_exits","unique_ips":0},` +
			`{"country":"WF","status":"no_daily_test","unique_ips":null}]}`,
		`{"country":null,"requested_countries":["NL","WF"],"groups":1,"country_status":[` +
			`{"country":"NL","status":"too_few_ips","unique_ips":3},` +
			`{"country":"WF","status":"no_exits","unique_ips":0}]}`,
	} {
		p := &snap.Product{}
		if err := json.Unmarshal([]byte(criteria), &p.Criteria); err != nil {
			t.Fatal(err)
		}
		groups = append(groups, p)
	}
	body, err := json.Marshal(requestedCountryStatus(groups))
	if err != nil {
		t.Fatal(err)
	}
	want := `[{"country":"NL","status":"too_few_ips","uniqueIps":3},` +
		`{"country":"TV","status":"no_exits","uniqueIps":0},` +
		`{"country":"WF","status":"no_exits","uniqueIps":0}]`
	if string(body) != want {
		t.Fatalf("got %s, want %s", body, want)
	}
}
