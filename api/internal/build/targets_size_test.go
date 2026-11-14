package build

import (
	"encoding/json"
	"sort"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// The /v1/targets size rule (api/answers/targets.md, "Size"): every body is
// at most targetsBodyMax, and the largest body's bytes per network at most
// targetsNetworkMax, so 21 networks at that share still fit under
// MaxAnswerBytes (9,528 at the week's shape with full-width success scores,
// deltas and the HTTP cells' challenge and block scores: a browser cell's one
// engine is about 640 bytes). Raised from 150 KB and 8,600 for the success
// scores (SUCCESS-SCORE-API-1, the architect's choice), from 160 KB and 8,900
// for the cells' success score deltas (SUCCESS-SCORE-DELTA-1, the same
// choice), then from 168 KB and 9,300 for the HTTP challenge and block scores
// (CHALLENGE-BLOCK-TIE-1, the 170 KB line the issue set).
const (
	targetsBodyMax    = 170 << 10
	targetsNetworkMax = 9700
)

// TestEveryTargetsBodyFitsWithRoom builds the /v1/targets bodies of 18
// networks at the week's shape (testscale.Week: 9 browser targets and the AI
// group's 3 sources, each on two engines) and at QA-1's (10 browser targets on
// one engine). Only the windows' targets shape them, so one requested
// country and a few exits are enough.
func TestEveryTargetsBodyFitsWithRoom(t *testing.T) {
	t.Parallel()
	for name, inputs := range map[string]func(testscale.Shape) (*store.Inputs, error){
		"week": testscale.WeekInputs, "QA-1": testscale.Inputs} {
		res := targetsScaleBuild(t, inputs)
		sizes, largest := map[string]int{}, ""
		for path, body := range res.Pages.Registered {
			if !strings.HasPrefix(path, "/v1/targets/") {
				continue
			}
			raw, err := json.Marshal(body)
			if err != nil {
				t.Fatal(err)
			}
			if sizes[path] = len(raw); largest == "" || len(raw) > sizes[largest] {
				largest = path
			}
		}
		if len(sizes) != 4*3*3 {
			t.Fatalf("%s: %d bodies", name, len(sizes))
		}
		paths := make([]string, 0, len(sizes))
		for p := range sizes {
			paths = append(paths, p)
		}
		sort.Strings(paths)
		for _, p := range paths {
			t.Logf("%s %s: %d bytes", name, p, sizes[p])
			if sizes[p] > targetsBodyMax {
				t.Errorf("%s %s is %d bytes, above %d", name, p, sizes[p], targetsBodyMax)
			}
		}
		nets := len(res.Pages.Registered[largest].(*TargetsAnswer).Networks)
		if per := sizes[largest] / nets; nets != 18 || per > targetsNetworkMax {
			t.Errorf("%s %s: %d bytes for %d networks, %d per network, above %d", name, largest, sizes[largest], nets, per, targetsNetworkMax)
		}
	}
}

// targetsScaleBuild is the build of 18 networks of inputs' shape.
func targetsScaleBuild(t *testing.T, inputs func(testscale.Shape) (*store.Inputs, error)) *Result {
	t.Helper()
	in, err := inputs(testscale.Shape{Networks: 18, Countries: 1, Exits: 3, Tail: 30, Browser: true, SuccessScores: true})
	if err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	return res
}

// No content is lost: a network's cell in each engine body is its network
// answer's engines.<engine> of the target (30d, the profile's window).
func TestTheEngineBodiesAreTheNetworkAnswersEngines(t *testing.T) {
	t.Parallel()
	res := targetsScaleBuild(t, testscale.WeekInputs)
	net := res.Pages.Network["net03/residential"]
	if net == nil || net.Profile == nil {
		t.Fatal("no network answer for net03")
	}
	checked := 0
	for _, engine := range TargetEngines {
		ans := res.Pages.Registered["/v1/targets/residential/30d/"+engine].(*TargetsAnswer)
		var cells map[string]*TargetCell
		for _, n := range ans.Networks {
			if n.Slug == "net03" {
				cells = n.Cells
			}
		}
		for _, tb := range net.Profile.Targets {
			if tb.BrowserBenchmark == nil {
				continue
			}
			e, c := tb.Engines[engine], cells[tb.Target]
			got := model.EngineBenchmark{Requests: c.Requests, Successes: e.Successes, Outcomes: c.Outcomes,
				PageLoadP50: c.PageLoadP50, PageLoadP90: c.PageLoadP90, Scores: c.Scores}
			want, _ := json.Marshal(e)
			if raw, _ := json.Marshal(got); !e.Requests.OK() || model.SV(c.Engine) != engine || c.SuccessRate != e.Outcomes.Ok || string(raw) != string(want) {
				t.Errorf("%s %s: cell %+v, network answer %+v", engine, tb.Target, c, e)
			}
			checked++
		}
	}
	if checked != 2*12 {
		t.Errorf("checked %d engine cells, want 24", checked)
	}
}
