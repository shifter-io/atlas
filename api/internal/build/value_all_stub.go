package build

// TEMPORARY stand-in for VALUE-ALL-1 (#888)'s value_all.go: removed when
// this branch rebases on it.

import (
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

const (
	gbBytes             = 1e9
	NANoSuccesses       = "no_successes"
	NAIncompleteTargets = "incomplete_targets"
)

type TargetCost struct {
	CostPer1k Num `json:"costPer1k"`
	Tries     Num `json:"tries"`
	Successes Num `json:"successes"`
	MbPerTry  Num `json:"mbPerTry"`
}

type HomeValueAllEnd struct {
	Slug      string `json:"slug"`
	Name      string `json:"name"`
	CostPer1k Num    `json:"costPer1k"`
}

type HomeValueAll struct {
	Best    *HomeValueAllEnd `json:"best"`
	Worst   *HomeValueAllEnd `json:"worst"`
	SpreadX Num              `json:"spreadX"`
}

func targetCost(price Num, m snap.Metrics) (TargetCost, string) {
	bytes := m.BytesUp + m.BytesDown
	tc := TargetCost{CostPer1k: nan, Tries: m.Completed, Successes: m.Successes, MbPerTry: nan}
	switch {
	case !m.Completed.OK() || m.Completed <= 0:
		return tc, NANoRun
	case !m.Successes.OK() || m.Successes <= 0:
		return tc, NANoSuccesses
	case !bytes.OK() || bytes <= 0:
		return tc, NANotMeasured
	}
	tc.MbPerTry = Num(bytes.F() / m.Completed.F() / 1e6)
	if !price.OK() {
		return tc, NACatalog
	}
	tc.CostPer1k = Num(price.F() * bytes.F() / gbBytes / m.Successes.F() * 1000)
	return tc, ""
}

func cardTargets(ts []model.TargetMeta) []string {
	out := []string{}
	for _, t := range ts {
		if isCard(t) {
			out = append(out, t.ID)
		}
	}
	return out
}
