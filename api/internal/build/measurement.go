package build

import (
	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
)

// Measurement is whether a network was measured in a range, or its standing
// is held while it is not (STANDING-FREEZE-1, atlas calculated/standing.py):
// Status measured; frozen (its score, ranks, figures and history are the last
// published values from before it stopped, ValuesAsOf their build's cutoff,
// FrozenSince the first build that froze them); or stopped (no earlier values:
// no score and no rank). LastMeasuredAt is the network's newest stored
// request. Every field is null for a network without a measured window, or a
// snapshot from before it.
type Measurement struct {
	Status         model.Str `json:"status"`
	LastMeasuredAt *string   `json:"lastMeasuredAt"`
	FrozenSince    *string   `json:"frozenSince"`
	ValuesAsOf     *string   `json:"valuesAsOf"`
}

// measurement is the measurement of a network's product window rangeID (sp
// nil: none).
func measurement(sp *snap.Product, rangeID string) Measurement {
	if sp == nil {
		return Measurement{}
	}
	s := sp.Window(rangeID).Standing
	if s == nil {
		return Measurement{}
	}
	return Measurement{Status: model.S(s.Status), LastMeasuredAt: isoTime(s.LastMeasured),
		FrozenSince: isoTime(s.FrozenSince), ValuesAsOf: isoTime(s.ValuesFrom)}
}

// isoTime is a snapshot time as Date.prototype.toISOString; nil when it is
// nil or not a time.
func isoTime(s *string) *string {
	if s == nil {
		return nil
	}
	return isoMs(snap.Ms(*s))
}
