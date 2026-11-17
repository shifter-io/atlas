package build

import (
	"sort"
	"strings"

	"github.com/shifter-io/atlas/api/internal/model"
)

// A network's place lists score each row as a /v1/places row is scored
// (placeScores, SUCCESS-SCORE-ALL-1): the network's success rate at the place
// ranked among the networks of its kind there. The build keeps only the
// places that can be ranked (scoreMinRanked networks with scoreMinRequests
// requests each) and each ranked network's rank there; a row's score is
// derived from that table when its page is cut, so a city or ASN list's rows
// are not kept for it.

// placeScoreKey is one place of a range's dimension among a kind's networks.
type placeScoreKey struct{ rangeID, dimension, kind, key string }

// placeRated is one network's success rate and requests at a place.
type placeRated struct {
	slug string
	rate ratedRate
}

// placeRank is one network's rank at a place.
type placeRank struct {
	slug string
	rank float64
}

// placeStanding is a rankable place's status (ranked; a tie is a shared
// first place, SUCCESS-TIE-1) and each ranked network's rank there.
type placeStanding struct {
	status string
	ranks  []placeRank
}

// placeRatings gathers, while a build merges its networks' lists, each
// network's rate at every place where it has scoreMinRequests requests: the
// only rows metricScores ranks.
type placeRatings map[placeScoreKey][]placeRated

// add adds one network's rows of a list.
func (p placeRatings) add(slug, kind string, list placeList, rows []PlaceRow) {
	for _, r := range rows {
		if r.SuccessRate.OK() && r.Requests.OK() && r.Requests >= scoreMinRequests {
			k := placeScoreKey{list.rangeID, list.dimension, kind, r.Key}
			p[k] = append(p[k], placeRated{slug, ratedRate{r.SuccessRate, r.Requests}})
		}
	}
}

// standings is the table of the places with at least scoreMinRanked rated
// networks, each ranked with metricScores.
func (p placeRatings) standings() map[placeScoreKey]placeStanding {
	out := map[placeScoreKey]placeStanding{}
	for k, rated := range p {
		if len(rated) < scoreMinRanked {
			continue
		}
		rates := make([]ratedRate, len(rated))
		for i, r := range rated {
			rates[i] = r.rate
		}
		scores := metricScores(rates)
		st := placeStanding{status: scores[0].Status}
		if st.status == scoreRanked {
			st.ranks = make([]placeRank, len(rated))
			for i, r := range rated {
				st.ranks[i] = placeRank{r.slug, scores[i].Rank.F()}
			}
		}
		out[k] = st
	}
	return out
}

// placeScore is a network's score at a place of a range's dimension: not
// enough data under scoreMinRequests requests (or without a rate), too few
// networks for a place not in the table, else the table's status and rank.
func (pg *Pages) placeScore(slug, kind, rangeID, dimension string, r PlaceRow) *model.MetricScore {
	out := &model.MetricScore{Score: nan, Rank: nan, RankedOf: nan, Status: notEnoughData}
	if !r.SuccessRate.OK() || !r.Requests.OK() || r.Requests < scoreMinRequests {
		return out
	}
	st, ok := pg.standings[placeScoreKey{rangeID, dimension, kind, r.Key}]
	if !ok {
		out.Status = scoreTooFewNetworks
		return out
	}
	if st.status != scoreRanked {
		out.Status = st.status
		return out
	}
	m, best := float64(len(st.ranks)), st.ranks[0].rank
	for _, n := range st.ranks {
		best = min(best, n.rank)
	}
	for _, n := range st.ranks {
		if n.slug == slug {
			out.Score, out.Rank, out.RankedOf, out.Status = shiftedRankScore(n.rank, best, m), Num(n.rank), Num(m), scoreRanked
		}
	}
	return out
}

// scoreRows sets the successScore of the rows of a network's list (scope
// "slug/kind"); a country's lists and the all-networks lists have none.
func (pg *Pages) scoreRows(scope, rangeID, dimension string, rows []PlaceRow) {
	slug, kind, ok := strings.Cut(scope, "/")
	if !ok || slug == "country" || slug == AllNetworks {
		return
	}
	for i := range rows {
		rows[i].SuccessScore = pg.placeScore(slug, kind, rangeID, dimension, rows[i])
	}
}

// scoreCountries scores the top countries of every network answer.
func (pg *Pages) scoreCountries() {
	for _, ans := range pg.Network {
		for rangeID, t := range ans.Ranges {
			pg.scoreRows(ans.Network.Slug+"/"+ans.Kind, rangeID, "country", t.Countries)
		}
	}
}

// withScoreNA is a place page's N/A paths, sorted, with their codes: paths
// (each not_measured_per_place) and its rows' null successScore score, rank
// and rankedOf, each with its status (the last row's, as naOf would).
func withScoreNA(paths []string, rows []PlaceRow) ([]string, []string) {
	status := map[string]string{}
	for _, r := range rows {
		if s := r.SuccessScore; s != nil {
			for _, f := range [...]struct {
				path string
				null bool
			}{{"rows[].successScore.score", !s.Score.OK()}, {"rows[].successScore.rank", !s.Rank.OK()},
				{"rows[].successScore.rankedOf", !s.RankedOf.OK()}} {
				if f.null {
					status[f.path] = s.Status
				}
			}
		}
	}
	for p := range status {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	codes := make([]string, len(paths))
	for i, p := range paths {
		codes[i] = NANotMeasuredPerPlace
		if c, ok := status[p]; ok {
			codes[i] = c
		}
	}
	return paths, codes
}
