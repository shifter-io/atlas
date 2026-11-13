package build

import (
	"maps"
	"slices"

	"github.com/Zeus-Development/atlas/api/internal/model"
)

// TargetEngines are the engines of the engine= bodies; "" is the default
// body, each network's main engine.
var TargetEngines = []string{"camoufox", "chrome"}

// engineView is nets with each cell in one engine ("" each cell's main
// engine), ranked.
func engineView(nets []TargetNetwork, targets []model.TargetMeta, engine string) []TargetNetwork {
	out := make([]TargetNetwork, len(nets))
	for i, n := range nets {
		out[i] = n
		out[i].Cells = make(map[string]*TargetCell, len(n.Cells))
		for id, c := range n.Cells {
			out[i].Cells[id] = viewCell(c, engine)
		}
	}
	for _, t := range targets {
		rankCells(out, t.ID)
	}
	return out
}

// viewCell is a copy of c in engine: on a browser target, that engine's
// figures (nulls and no tries when it has none); "" keeps the main engine's.
func viewCell(c *TargetCell, engine string) *TargetCell {
	v := *c
	if c.BrowserCell == nil {
		return &v
	}
	b := *c.BrowserCell
	if engine != "" {
		b.Engine = model.S(engine)
		e, ok := c.engines[engine]
		if !ok || !(e.Requests > 0) {
			e = model.EngineBenchmark{Requests: 0, Outcomes: naOutcomes, PageLoadP50: nan, PageLoadP90: nan}
			v.CostPer1kSuccess = nan
		}
		v.SuccessRate, v.CaptchaRate, v.BlockRate, v.Requests = e.Outcomes.Ok, e.Outcomes.Challenge, e.Outcomes.Block, e.Requests
		b.Outcomes, b.PageLoadP50, b.PageLoadP90, v.Scores = e.Outcomes, e.PageLoadP50, e.PageLoadP90, e.Scores
	}
	b.OtherEngines = []string{}
	for _, name := range slices.Sorted(maps.Keys(c.engines)) {
		if name != model.SV(b.Engine) && c.engines[name].Requests > 0 {
			b.OtherEngines = append(b.OtherEngines, name)
		}
	}
	v.BrowserCell = &b
	return &v
}
