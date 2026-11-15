package build

import (
	"errors"
	"net/http"
	"slices"
	"sort"

	"github.com/Zeus-Development/atlas/api/internal/model"
)

// /v1/meta (api/answers/meta.md): the build's reference data every page
// shares, one body per build.

const metaPath = "/v1/meta"

func init() {
	RegisterAnswer(PageAnswer{Name: "meta", Route: "GET " + metaPath, Build: buildMeta, Samples: []string{metaPath},
		InDataset: true, Request: func(p string) string { return queryRequest(p, metaPath) },
		Key: func(r *http.Request) (string, error) {
			_, err := Query(r)
			return metaPath, err
		}})
}

// MetaAnswer is /v1/meta.
type MetaAnswer struct {
	GeneratedAt  string                 `json:"generatedAt"`
	Snapshot     MetaSnapshot           `json:"snapshot"`
	Weights      model.Breakdown        `json:"weights"`
	Profiles     []model.RankingProfile `json:"profiles"`
	Grades       []GradeBand            `json:"grades"` // nil when the snapshot publishes none
	Targets      []MetaTarget           `json:"targets"`
	TargetCounts TargetCounts           `json:"targetCounts"`
	Ranges       []MetaRange            `json:"ranges"`
	WindowHours  Num                    `json:"windowHours"` // the shortest methodology window; null when nothing is published
	Scopes       []model.TestScope      `json:"scopes"`
	// NAList lists the answer's own null fields and empty profiles;
	// targets, profiles and scopes carry their own.
	NAList
}

// MetaSnapshot names the published calculation (all null without one).
type MetaSnapshot struct {
	ID            model.Str `json:"id"`
	AsOf          model.Str `json:"asOf"`
	MethodologyID model.Str `json:"methodologyId"`
	// MethodologyURL is Dataset.snapshot.methodologyUrl: the path of the
	// rules file whose SHA-256 is MethodologyID (methodology_file.go).
	MethodologyURL model.Str `json:"methodologyUrl"`
}

// MetaRange is a range and the length of the methodology window it reads.
type MetaRange struct {
	RangeMeta
	// Hours is the range's window in the snapshot's methodology windows
	// (its days in hours, such as 168 for 7d); null when the snapshot
	// has no such window or nothing is published.
	Hours Num `json:"hours"`
}

// GradeBand is one grade and the lowest score (0-100) that earns it.
type GradeBand struct {
	Grade string `json:"grade"`
	Min   Num    `json:"min"`
}

// MetaTarget is a target's meta, its scoring weight and whether it is a card.
type MetaTarget struct {
	model.TargetMeta
	// ScoreWeight is the target's difficulty in the browser overall score
	// (methodology [browser_difficulty]); null for a target it does not weigh.
	ScoreWeight Num `json:"scoreWeight"`
	// Card is true for a measured target outside any group: the ones a
	// client shows as cards.
	Card bool `json:"card"`
}

func buildMeta(c *Context) (map[string]any, error) {
	ds := c.Dataset
	if ds == nil {
		return nil, errors.New("no dataset")
	}
	m := MetaAnswer{GeneratedAt: c.GeneratedAt, Weights: ds.Weights, Profiles: ds.Profiles, Scopes: ds.Scopes,
		Snapshot: MetaSnapshot{ID: ds.Snapshot.SnapshotID, AsOf: ds.Snapshot.AsOf, MethodologyID: ds.Snapshot.MethodologyID,
			MethodologyURL: ds.Snapshot.MethodologyURL}}
	var windows []float64
	if c.Snapshot != nil {
		m.Grades = gradeBands(c.Snapshot.Methodology.Settings.Grades)
		windows = c.Snapshot.Methodology.Settings.Windows
	}
	m.Ranges, m.WindowHours = metaRanges(windows)
	var err error
	if m.Targets, m.TargetCounts, err = metaTargets(ds.Targets, scoreWeights(c.Snapshot)); err != nil {
		return nil, err
	}
	f := naFacts{measured: true, ran: true, priced: true}
	code := func(p string) string {
		if p == "snapshot.methodologyUrl" && ds.Snapshot.Available {
			return NAMethodologyFileUnknown
		}
		return f.code(p)
	}
	if err = m.NAList.list(m, []string{"profiles"}, code, "profiles", "targets", "scopes"); err != nil {
		return nil, err
	}
	return map[string]any{metaPath: m}, nil
}

// metaRanges is every range with its window hours, and the shortest window
// (NaN without any).
func metaRanges(windows []float64) ([]MetaRange, Num) {
	out := make([]MetaRange, 0, len(Ranges))
	for _, r := range Ranges {
		mr := MetaRange{RangeMeta: r, Hours: nan}
		if slices.Contains(windows, float64(r.Days*24)) {
			mr.Hours = Num(r.Days * 24)
		}
		out = append(out, mr)
	}
	if len(windows) == 0 {
		return out, nan
	}
	return out, Num(slices.Min(windows))
}

// gradeBands is the grades from the highest band down; nil without any.
func gradeBands(grades map[string]float64) []GradeBand {
	if len(grades) == 0 {
		return nil
	}
	out := make([]GradeBand, 0, len(grades))
	for g, score := range grades {
		out = append(out, GradeBand{Grade: g, Min: Num(score)})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Min != out[j].Min {
			return out[i].Min > out[j].Min
		}
		return out[i].Grade < out[j].Grade
	})
	return out
}

// metaTargets adds each target's scoring weight and card flag, in dataset
// order, and counts them (targetCounts). A target the browser score does not
// weigh (an HTTP or catalog-only one) has a null weight by design, so only a
// browser target's missing weight is in its na.
func metaTargets(targets []model.TargetMeta, weights map[string]Num) ([]MetaTarget, TargetCounts, error) {
	out := make([]MetaTarget, 0, len(targets))
	counts := targetCounts(targets)
	for _, t := range targets {
		mt := MetaTarget{TargetMeta: t, ScoreWeight: nan, Card: isCard(t)}
		if w, ok := weights[t.ID]; ok {
			mt.ScoreWeight = w
		}
		var skip []string
		if model.SV(t.Engine) != model.EngineBrowser {
			skip = []string{"scoreWeight"}
		}
		var err error
		if mt.NA, err = naOf(mt, nil, false, skip...); err != nil {
			return nil, counts, err
		}
		out = append(out, mt)
	}
	return out, counts, nil
}
