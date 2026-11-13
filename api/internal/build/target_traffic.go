package build

import (
	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// TargetTraffic is one target's traffic in a range (ranges.<range>.targets[],
// TARGET-TRAFFIC-1): its completed requests (a browser target's tries) and
// its measured wire bytes, both directions, ÷ 10⁶ (decimal MB, as
// stats.<range>.bandwidthMb). bandwidthMb is null without a request in the
// range (no_run_in_window) or without recorded bytes (not_measured).
type TargetTraffic struct {
	Target      string `json:"target"`
	Requests    Num    `json:"requests"`
	BandwidthMb Num    `json:"bandwidthMb"`
}

// ProfileTarget is a profile target's benchmark with its measured traffic in
// the profile's range (30d), as TargetTraffic's bandwidthMb.
type ProfileTarget struct {
	model.TargetBenchmark
	BandwidthMb Num `json:"bandwidthMb"`
}

// UnmarshalJSON decodes the benchmark and bandwidthMb (the embedded
// benchmark's own decoder would drop it).
func (v *ProfileTarget) UnmarshalJSON(d []byte) error {
	if err := v.TargetBenchmark.UnmarshalJSON(d); err != nil {
		return err
	}
	var own struct {
		BandwidthMb Num `json:"bandwidthMb"`
	}
	if err := jsnum.DecodeNA(d, &own); err != nil {
		return err
	}
	v.BandwidthMb = own.BandwidthMb
	return nil
}

func profileTargets(benchmarks []model.TargetBenchmark) []ProfileTarget {
	out := make([]ProfileTarget, len(benchmarks))
	for i, b := range benchmarks {
		out[i] = ProfileTarget{TargetBenchmark: b, BandwidthMb: nan}
	}
	return out
}

// targetTraffic is every target's traffic in the groups' window rangeID, in
// targets' order, summed over the groups. A group target's is its sources'
// sum: the snapshot keeps a group's one engine only (calculated/browser.py),
// so the sources' sum is every try of the group, and a total counts the group
// or its sources, never both.
func targetTraffic(groups []*snap.Product, rangeID string, targets []model.TargetMeta) []TargetTraffic {
	out := make([]TargetTraffic, 0, len(targets))
	for _, t := range targets {
		ids := t.Sources
		if len(ids) == 0 {
			ids = []string{t.ID}
		}
		requests, bytes := Num(0), nan
		for _, id := range ids {
			for _, p := range groups {
				m := p.Window(rangeID).Targets[id].Metrics
				requests, bytes = addNum(requests, m.Completed), addNum(bytes, wireBytes(m))
			}
		}
		out = append(out, TargetTraffic{Target: t.ID, Requests: requests, BandwidthMb: bytes / 1e6})
	}
	return out
}

// addTargetTraffic sets every network answer's ranges.<range>.targets and the
// bandwidthMb of its profile's targets (the profile's product, 30d).
func (pg *Pages) addTargetTraffic(entries []*entry, targets []model.TargetMeta) {
	for _, ans := range pg.Network {
		for rangeID, t := range ans.Ranges {
			t.Targets = targetTraffic(ans.groups, rangeID, targets)
		}
	}
	for _, e := range entries {
		for kind, sp := range e.Snap {
			ans := pg.Network[e.P.Slug+"/"+kind]
			if sp == nil || ans == nil || ans.Profile == nil {
				continue
			}
			month := map[string]Num{}
			for _, t := range targetTraffic([]*snap.Product{sp}, "30d", targets) {
				month[t.Target] = t.BandwidthMb
			}
			for i := range ans.Profile.Targets {
				if mb, ok := month[ans.Profile.Targets[i].Target]; ok {
					ans.Profile.Targets[i].BandwidthMb = mb
				}
			}
		}
	}
}

// noRunTargets gives no_run_in_window to the bandwidthMb under prefix of each
// target (its requests by id) without a request: one with requests but no
// bytes keeps the figure's not_measured.
func noRunTargets(codes map[string]string, prefix string, requests map[string]Num) {
	for id, n := range requests {
		if !n.OK() || n <= 0 {
			codes[prefix+"targets["+id+"].bandwidthMb"] = NANoRun
		}
	}
}

func profileRequests(targets []ProfileTarget) map[string]Num {
	out := make(map[string]Num, len(targets))
	for _, t := range targets {
		out[t.Target] = t.Requests
	}
	return out
}

func rangeRequests(targets []TargetTraffic) map[string]Num {
	out := make(map[string]Num, len(targets))
	for _, t := range targets {
		out[t.Target] = t.Requests
	}
	return out
}
