package build

import (
	"time"
)

// The home's network curves share one time axis per range (SPARK-TIMES-1):
// each network's score curve has a value per hour or day of the range's
// curve axis (home_curve.go), null where the network has no request, so the
// answer holds one times list for every curve instead of one per network.

// homeHistory is a ranking row's success and latency (p50) histories for the
// range as curve points; empty for a row without an entry or window stats.
type homeHistory struct {
	curve, latency []curvePoint
	scores         []scorePoint
}

// homeHistories is each row's homeHistory, in the rows' order; asOf places
// the curve points of a row without a snapshot product.
func homeHistories(rows []RankingRow, bySlug map[string]*entry, stats map[string]map[string]*WindowStats, rangeID string, asOf time.Time) []homeHistory {
	out := make([]homeHistory, len(rows))
	for i, row := range rows {
		e, s := bySlug[row.Slug], stats[row.Slug][rangeID]
		if e == nil || s == nil {
			continue
		}
		out[i].curve, out[i].latency = entryCurvePoints(e, s, rangeID, asOf), entryLatencyPoints(e, s, rangeID, asOf)
		out[i].scores = entryScorePoints(e, rangeID)
	}
	return out
}
