package build

import "strings"

// PlaceAnswer is /v1/places/{region|city}/{key}: every network's row of one
// region or city in one range, ranked among the networks of the same kind
// (IPs: more is better; latency: lower is better), as a country's answer
// ranks its networks, in list order. Key is the place key of the place lists.
type PlaceAnswer struct {
	GeneratedAt string     `json:"generatedAt"`
	Dimension   string     `json:"dimension"`
	Key         string     `json:"key"`
	Range       string     `json:"range"`
	Rows        []PlaceRow `json:"rows"`
}

// Place is a region or city page of a v4 build; ok is false when no network
// has a row with that key in that range (or the build is older).
func (pg *Pages) Place(generatedAt, dimension, key, rangeID string) (*PlaceAnswer, bool) {
	cc, _, _ := strings.Cut(key, "|")
	if !pg.v4 || dimension != "region" && dimension != "city" || cc == "" {
		return nil, false
	}
	out := &PlaceAnswer{GeneratedAt: generatedAt, Dimension: dimension, Key: key, Range: rangeID, Rows: []PlaceRow{}}
	for _, r := range mergeSources(pg.sources[placeList{rangeID, dimension}], dimension, cc, true) {
		if r.Key == key {
			out.Rows = append(out.Rows, r)
		}
	}
	if len(out.Rows) == 0 {
		return nil, false
	}
	rankPlaces(out.Rows)
	return out, true
}
