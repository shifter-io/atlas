package build

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

// sameMetric reports whether two scores are alike (NaN equal to NaN).
func sameMetric(a, b *model.MetricScore) bool {
	if a == nil || b == nil {
		return a == b
	}
	eq := func(x, y Num) bool { return x == y || !x.OK() && !y.OK() }
	return a.Status == b.Status && eq(a.Score, b.Score) && eq(a.Rank, b.Rank) && eq(a.RankedOf, b.RankedOf)
}

// A day is ranked among the networks of its kind with that day: three
// ranked networks give 10, 5.5 and 1; a day of two networks is
// too_few_networks, a day under 100 requests not_enough_data; the
// all-networks answer's days have none, not_applicable.
func TestDaysAreRankedAmongTheNetworksWithThatDay(t *testing.T) {
	t.Parallel()
	day := func(d string, requests, rate Num) DayRow {
		return DayRow{Day: d, Requests: requests, SuccessRate: rate}
	}
	net := func(slug, kind string, days ...DayRow) *NetworkAnswer {
		return &NetworkAnswer{Network: NetworkRow{Slug: slug}, Kind: kind, Days: days}
	}
	pg := &Pages{Network: map[string]*NetworkAnswer{
		"a/residential":                 net("a", "residential", day("d1", 1000, 0.9), day("d2", 1000, 0.9), day("d3", 50, 0.9)),
		"b/residential":                 net("b", "residential", day("d1", 1000, 0.7), day("d2", 1000, 0.7), day("d3", 1000, 0.7)),
		"c/residential":                 net("c", "residential", day("d1", 1000, 0.5), day("d3", 1000, 0.5)),
		"d/datacenter":                  net("d", "datacenter", day("d1", 1000, 0.1)),
		"e/residential":                 net("e", "residential"),
		AllNetworks + "/" + AllNetworks: net(AllNetworks, AllNetworks, day("d1", 4000, 0.5)),
	}}
	pg.scoreDays()
	ranked := func(score, rank Num) *model.MetricScore {
		return &model.MetricScore{Score: score, Rank: rank, RankedOf: 3, Status: scoreRanked}
	}
	none := func(status string) *model.MetricScore {
		return &model.MetricScore{Score: nan, Rank: nan, RankedOf: nan, Status: status}
	}
	for key, want := range map[string][]*model.MetricScore{
		"a/residential": {ranked(10, 1), none(scoreTooFewNetworks), none(notEnoughData)},
		"b/residential": {ranked(5.5, 2), none(scoreTooFewNetworks), none(scoreTooFewNetworks)},
		"c/residential": {ranked(1, 3), none(scoreTooFewNetworks)},
		"d/datacenter":  {none(scoreTooFewNetworks)},
	} {
		for i, w := range want {
			if got := pg.Network[key].Days[i].SuccessScore; !sameMetric(got, w) {
				t.Errorf("%s day %d: %+v, want %+v", key, i, got, w)
			}
		}
	}
	all := pg.Network[AllNetworks+"/"+AllNetworks]
	if all.Days[0].SuccessScore != nil {
		t.Errorf("all-networks day %+v, want no score", all.Days[0].SuccessScore)
	}
	if err := all.listNA(nil); err != nil {
		t.Fatal(err)
	}
	if got := all.ReasonOf("days[].successScore"); got != NANotApplicable {
		t.Errorf("all-networks days[].successScore reason %q, want %s", got, NANotApplicable)
	}
}

// The table keeps only the places with three networks of scoreMinRequests:
// a place of two such networks (and one under the minimum) is left out, and
// a row's score from the table is placeScores' on the same rows.
func TestPlaceStandingsKeepOnlyRankablePlaces(t *testing.T) {
	t.Parallel()
	rows := map[string][]PlaceRow{ // network -> its rows
		"a": {{Key: "ranked", Requests: 500, SuccessRate: 0.95}, {Key: "two", Requests: 500, SuccessRate: 0.9},
			{Key: "similar", Requests: 5000, SuccessRate: 0.8}, {Key: "tie", Requests: 500, SuccessRate: 0.9}},
		"b": {{Key: "ranked", Requests: 400, SuccessRate: 0.6}, {Key: "two", Requests: 500, SuccessRate: 0.5},
			{Key: "similar", Requests: 5000, SuccessRate: 0.8}, {Key: "tie", Requests: 500, SuccessRate: 0.9}},
		"c": {{Key: "ranked", Requests: 300, SuccessRate: 0.2}, {Key: "two", Requests: 99, SuccessRate: 0.1},
			{Key: "similar", Requests: 5000, SuccessRate: 0.81}, {Key: "tie", Requests: 500, SuccessRate: 0.1}},
		"d": {{Key: "ranked", Requests: nan, SuccessRate: nan}},
	}
	rated := placeRatings{}
	list := placeList{"30d", "city"}
	for _, slug := range []string{"a", "b", "c", "d"} {
		rated.add(slug, "residential", list, rows[slug])
	}
	pg := &Pages{standings: rated.standings()}
	if _, ok := pg.standings[placeScoreKey{"30d", "city", "residential", "two"}]; ok || len(pg.standings) != 3 {
		t.Fatalf("standings %+v, want ranked, similar and tie only", pg.standings)
	}
	if st := pg.standings[placeScoreKey{"30d", "city", "residential", "similar"}]; st.status != scoreAllSimilar || st.ranks != nil {
		t.Errorf("similar standing %+v, want all_similar without ranks", st)
	}
	byKey := map[string][]PlaceRow{}
	for _, slug := range []string{"a", "b", "c", "d"} {
		for _, r := range rows[slug] {
			r.Network, r.Kind = slug, "residential"
			byKey[r.Key] = append(byKey[r.Key], r)
		}
	}
	for key, at := range byKey {
		placeScores(at)
		for _, r := range at {
			if got := pg.placeScore(r.Network, "residential", "30d", "city", r); !sameMetric(got, r.SuccessScore) {
				t.Errorf("%s at %s: %+v, want %+v", r.Network, key, got, r.SuccessScore)
			}
		}
	}
	if got := pg.placeScore("a", "datacenter", "30d", "city", rows["a"][0]); got.Status != scoreTooFewNetworks {
		t.Errorf("another kind's score %+v, want too_few_networks", got)
	}
	if got := pg.placeScore("z", "residential", "30d", "city", rows["a"][0]); got.Status != notEnoughData {
		t.Errorf("an unranked network's score %+v, want not_enough_data", got)
	}
}

// Every row of a network's lists, of each dimension and range, on every page
// and in a search, and each network answer's top countries, has the score
// placeScores gives its network among the networks' rows with that key (the
// rows of /v1/places/{dim}/{key}); a country's and the all-networks lists
// have none.
func TestNetworkListRowsHaveTheirPlaceScore(t *testing.T) {
	t.Parallel()
	res, _ := smallNetBuild(t)
	pg, ctx := res.Pages, context.Background()
	want, statuses := wantPlaceScores(t, pg), map[string]bool{}
	for key, ans := range pg.Network {
		for _, r := range Ranges {
			for _, d := range placeDimensions {
				scoredPages(t, pg, key, r.ID, d, want, statuses)
			}
			for _, row := range ans.Ranges[r.ID].Countries {
				if w := want[key+"\x00"+r.ID+"\x00country\x00"+row.Key]; !sameMetric(row.SuccessScore, w) {
					t.Fatalf("%s %s country %s: %+v, want %+v", key, r.ID, row.Key, row.SuccessScore, w)
				}
			}
		}
	}
	if !statuses[scoreAllSimilar] || !statuses[scoreTooFewNetworks] {
		t.Errorf("statuses %v, want all_similar and too_few_networks rows", statuses)
	}
	place, ok, err := pg.Place(ctx, "", "city", pg.lists[listKey{"net01/residential", "30d", "city"}].first[0].Key, "30d")
	if err != nil || !ok {
		t.Fatal("no city page", err)
	}
	for _, r := range place.Rows {
		if w := want[r.Network+"/residential\x0030d\x00city\x00"+r.Key]; !sameMetric(r.SuccessScore, w) {
			t.Errorf("place row %s: %+v, want the list's %+v", r.Network, r.SuccessScore, w)
		}
	}
	found, _, _ := pg.List(ctx, "", "net00/residential", "30d", "asn", "AS64513", 1)
	if len(found.Rows) != 1 || !sameMetric(found.Rows[0].SuccessScore, want["net00/residential\x0030d\x00asn\x00"+found.Rows[0].Key]) {
		t.Errorf("asn search %+v", found.Rows)
	}
	cities, _, _ := pg.List(ctx, "", "country/"+pg.Countries.Countries[0].CountryCode, "30d", "city", "", 1)
	for _, rows := range [][]PlaceRow{cities.Rows, pg.Country[pg.Countries.Countries[0].CountryCode].Ranges["30d"]} {
		for _, r := range rows {
			if r.SuccessScore != nil {
				t.Fatalf("country row %+v has a score", r)
			}
		}
	}
}

// wantPlaceScores is placeScores of every network list row among the rows of
// every network with its key, by scope, range, dimension and key.
func wantPlaceScores(t *testing.T, pg *Pages) map[string]*model.MetricScore {
	t.Helper()
	groups := map[string][]PlaceRow{}
	for key := range pg.Network {
		slug, kind, _ := strings.Cut(key, "/")
		for _, r := range Ranges {
			for _, d := range placeDimensions {
				l := pg.lists[listKey{key, r.ID, d}]
				if l == nil || slug == AllNetworks {
					continue
				}
				rows, err := l.rows(context.Background(), nil)
				if err != nil {
					t.Fatal(err)
				}
				for _, row := range rows {
					row.Network, row.Kind = key, kind
					groups[r.ID+"\x00"+d+"\x00"+row.Key] = append(groups[r.ID+"\x00"+d+"\x00"+row.Key], row)
				}
			}
		}
	}
	out := map[string]*model.MetricScore{}
	for at, rows := range groups {
		placeScores(rows)
		for _, r := range rows {
			out[r.Network+"\x00"+at] = r.SuccessScore
		}
	}
	return out
}

// scoredPages checks every page of one list against want, and its na: a
// null score's reason is the last such row's status.
func scoredPages(t *testing.T, pg *Pages, scope, rangeID, dimension string, want map[string]*model.MetricScore, statuses map[string]bool) {
	t.Helper()
	for page := 1; ; page++ {
		out, ok, err := pg.List(context.Background(), "", scope, rangeID, dimension, "", page)
		if err != nil || !ok {
			t.Fatalf("%s %s %s page %d: %v", scope, rangeID, dimension, page, err)
		}
		last := ""
		for _, r := range out.Rows {
			w := want[scope+"\x00"+rangeID+"\x00"+dimension+"\x00"+r.Key] // none on the all-networks lists
			if !sameMetric(r.SuccessScore, w) {
				t.Fatalf("%s %s %s page %d row %s: %+v, want %+v", scope, rangeID, dimension, page, r.Key, r.SuccessScore, w)
			}
			if w != nil && !w.Score.OK() {
				last, statuses[w.Status] = w.Status, true
			}
		}
		if got := out.ReasonOf("rows[].successScore.score"); got != last {
			t.Fatalf("%s %s %s page %d: score reason %q, want %q", scope, rangeID, dimension, page, got, last)
		}
		if page >= out.Pages {
			return
		}
	}
}

// A dataset list row ends with its success score, status first: a ranked
// row's with its score, rank and rankedOf, another's status alone.
func TestADatasetRowEndsWithItsSuccessScore(t *testing.T) {
	t.Parallel()
	row := PlaceRow{Key: "AA", CountryCode: model.S("AA"), IpsSum: 1, Requests: 500, Successes: 400, LatencyP50: nan}
	first := []PlaceRow{row, row}
	first[1].Requests = 50
	pg := &Pages{standings: map[placeScoreKey]placeStanding{{"30d", "country", "residential", "AA"}: {status: scoreRanked,
		ranks: []placeRank{{"a", 1}, {"b", 2}, {"c", 3}}}}}
	k := &datasetKind{kind: "residential", pg: pg, slug: "a"}
	b, err := json.Marshal(placeFirst(&placeIndex{total: 2, first: first}, "country", k.scorer("30d", "country")))
	want := `{"total":2,"pages":1,"rows":[["AA",null,1,500,400,null,["ranked",10,1,3]],["AA",null,1,50,400,null,["not_enough_data"]]]}`
	if err != nil || string(b) != want {
		t.Errorf("%v: %s, want %s", err, b, want)
	}
}
