package build

import (
	"maps"
	"slices"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// isScorePart reports whether a path is a cell's score field its status
// explains: a score, rank and rankedOf are null unless the status is ranked
// (TARGET-SCORES-1).
func isScorePart(p string) bool {
	p = "." + p
	i := strings.LastIndex(p, ".scores.")
	if i < 0 {
		return false
	}
	parts := strings.Split(p[i+len(".scores."):], ".")
	return len(parts) == 2 && (parts[1] == "score" || parts[1] == "rank" || parts[1] == "rankedOf")
}

// triedTargets is every target with tries in sp's longest window, and every
// engine with tries of a browser target as "<target>/<engine>": a cell of
// another target or engine is not measured in production's cadence.
func triedTargets(sp *snap.Product) map[string]bool {
	out := map[string]bool{}
	for id, t := range sp.Window(Ranges[len(Ranges)-1].ID).Targets {
		out[id] = t.Metrics.Completed > 0
		if t.Browser == nil {
			continue
		}
		for name, e := range t.Browser.Engines {
			out[id+"/"+name] = e.Completed > 0
		}
	}
	return out
}

// listNA lists every target's, network's and cell's nulls in the engine view
// ("" each cell's main engine) once for all of them, as a list's elements are
// listed (targets[].difficulty, networks[].rank,
// networks[].cells.<target>.costPer1kSuccess): a body has 13 cells a network,
// so per-cell lists would grow it by a kilobyte a network. A path's reason is
// the most common one among the entities where it is null.
func (a *TargetsAnswer) listNA(engine string) error {
	votes := map[string]map[string]int{}
	add := func(prefix string, entity any, f naFacts, skip ...string) error {
		var l NAList
		if err := l.list(entity, nil, f.code, skip...); err != nil {
			return err
		}
		for _, p := range l.NA {
			if isScorePart(p) {
				continue
			}
			if votes[prefix+p] == nil {
				votes[prefix+p] = map[string]int{}
			}
			votes[prefix+p][l.ReasonOf(p)]++
		}
		return nil
	}
	for i := range a.Targets {
		// A catalog-only target has no engine or weight: it is not measured.
		if err := add("targets[].", &a.Targets[i], naFacts{measured: a.Targets[i].measured, ran: true, priced: true}, "scoreStatus"); err != nil {
			return err
		}
	}
	for i := range a.Networks {
		n := &a.Networks[i]
		for id, c := range n.Cells {
			tried := id
			if engine != "" && c.BrowserCell != nil {
				tried += "/" + engine
			}
			f := naFacts{measured: n.tried[tried], ran: c.Requests.OK() && c.Requests > 0, priced: n.price.OK()}
			if err := add("networks[].cells."+id+".", c, f); err != nil {
				return err
			}
		}
		if err := add("networks[].", n, naFacts{measured: true, ran: true, priced: n.price.OK()}, "cells"); err != nil {
			return err
		}
	}
	paths := slices.Sorted(maps.Keys(votes))
	codes := make([]string, len(paths))
	for i, p := range paths {
		for c, n := range votes[p] {
			if n > votes[p][codes[i]] || n == votes[p][codes[i]] && c < codes[i] {
				codes[i] = c
			}
		}
	}
	a.NAList.set(paths, codes)
	return nil
}
