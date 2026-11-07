package build

import (
	"context"
	"strings"
)

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

// Place is a region or city page; ok is false when no network has a row
// with that key in that range. The rows
// are read from the database unless cached.
func (pg *Pages) Place(ctx context.Context, generatedAt, dimension, key, rangeID string) (*PlaceAnswer, bool, error) {
	cc, _, _ := strings.Cut(key, "|")
	if dimension != "region" && dimension != "city" || cc == "" {
		return nil, false, nil
	}
	out := &PlaceAnswer{GeneratedAt: generatedAt, Dimension: dimension, Key: key, Range: rangeID, Rows: []PlaceRow{}}
	rows, err := mergeSources(ctx, pg.sources[placeList{rangeID, dimension}], dimension, cc, true)
	if err != nil {
		return nil, false, err
	}
	for _, r := range rows {
		if r.Key == key {
			out.Rows = append(out.Rows, r)
		}
	}
	if len(out.Rows) == 0 {
		return nil, false, nil
	}
	rankPlaces(out.Rows)
	return out, true, nil
}
