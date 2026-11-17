package build

import (
	"encoding/json"
	"maps"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

func TestEveryNAReasonCode(t *testing.T) {
	t.Parallel()
	all := naFacts{measured: true, ran: true, priced: true, claimed: true}
	unpriced := naFacts{measured: true, ran: true, claimed: true}
	for _, c := range []struct {
		f          naFacts
		path, want string
	}{
		{all, "listRate", NACatalog},
		{all, "pricing.trialKind", NACatalog},
		{all, "pricing.hasTrial", NACatalog},
		{all, "ranks.listRate.rank", NACatalog},
		{all, "kycVerified", NACatalog},
		{unpriced, "costPer1kSuccess", NACatalog},
		{unpriced, "costRank.rank", NACatalog},
		{unpriced, "cost.allTargets.cheapest", NACatalog},
		{all, "costPer1kSuccess", NAInsufficient}, // priced, no wire bytes
		{naFacts{measured: true, ran: true, priced: true}, "claimShare", NACatalog},
		{all, "claimShare", NAInsufficient},
		{naFacts{}, "successRate", NANotMeasured},
		{all, "latencyP99", NANotMeasured},
		{all, "market.medianLatency", NANotMeasured},
		{all, "blacklistHitRate", NANotMeasured},
		// MEASURED-FIELDS-1: never tested, run or not; exit addresses never
		// published; the payload without wire bytes; a price rank without a price.
		{naFacts{measured: true}, "trust.blacklistHitRate", NANotMeasured},
		{naFacts{measured: true}, "trust.leakChecks", NANotMeasured},
		{naFacts{measured: true, priced: true}, "trust.stickinessHoldRate", NANotMeasured},
		{naFacts{}, "ipSamples", NANotPublished},
		{all, "ipSamples", NANotPublished},
		{all, "pricing.avgPayloadKb", NANotMeasured},
		{naFacts{measured: true}, "pricing.avgPayloadKb", NANoRun},
		{unpriced, "pricing.headlinePriceRank", NACatalog},
		{all, "founded", NACatalog},
		{all, "observedDays", NAHistoryCoversRange},
		{naFacts{measured: true}, "observedDays", NANoRun},
		{naFacts{measured: true}, "successRate", NANoRun},
		{naFacts{measured: true}, "uptimePct", NANoRun},
		// No run in the window comes before the cadence, evidence and
		// calculator reasons (NO-RUN-WINDOW-1), not before the catalog's.
		{naFacts{measured: true, priced: true}, "latencyP50", NANoRun},
		{naFacts{measured: true, priced: true}, "uniqueIps", NANoRun},
		{naFacts{measured: true, priced: true}, "downtimeMinutes", NANoRun},
		{naFacts{measured: true, priced: true}, "captchaRate", NANoRun},
		{naFacts{measured: true, priced: true}, "costPer1kSuccess", NANoRun},
		{naFacts{measured: true}, "listRate", NACatalog},
		{all, "uptimePct", NANoEvidence},
		{all, "ranks.uptimePct.rank", NANoEvidence},
		{all, "downtimeMinutes", NANoEvidence},
		{all, "captchaRate", NAInsufficient},
		{all, "score", NAInsufficient},
	} {
		if got := c.f.code(c.path); got != c.want {
			t.Errorf("%+v %s: %s, want %s", c.f, c.path, got, c.want)
		}
	}
}

func TestAnNAListKeepsItsCommonReasonOnce(t *testing.T) {
	t.Parallel()
	type entity struct {
		A, B, C Num
		D       []string
		NAList
	}
	e := entity{A: nan, B: nan, C: 1, D: []string{}}
	code := func(p string) string { return map[string]string{"A": NACatalog, "B": NACatalog, "D": NANoRun}[p] }
	if err := e.NAList.list(e, []string{"D"}, code); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(e.NA, []string{"A", "B", "D"}) || e.Reason != NACatalog || !maps.Equal(e.Reasons, map[string]string{"D": NANoRun}) {
		t.Fatalf("na %v reason %s reasons %v", e.NA, e.Reason, e.Reasons)
	}
	if e.ReasonOf("A") != NACatalog || e.ReasonOf("D") != NANoRun || e.ReasonOf("C") != "" {
		t.Errorf("ReasonOf: %s %s %q", e.ReasonOf("A"), e.ReasonOf("D"), e.ReasonOf("C"))
	}
	e = entity{D: []string{"x"}}
	if err := e.NAList.list(e, []string{"D"}, code); err != nil {
		t.Fatal(err)
	}
	if raw, _ := json.Marshal(e.NAList); string(raw) != `{"na":[]}` {
		t.Errorf("no N/A: %s", raw)
	}
}

// Overlap pairs are computed in every range, 24h too: a null of a network
// with a run is insufficient_data, of one with no run in the range
// no_run_in_window, the market's insufficient_data (NO-RUN-WINDOW-1).
func TestOverlapNAIsNoRunOnlyWithoutARun(t *testing.T) {
	t.Parallel()
	for _, rangeID := range []string{"24h", "7d"} {
		for ran, want := range map[bool]string{true: NAInsufficient, false: NANoRun} {
			a := &OverlapAnswer{Range: rangeID, Networks: []OverlapNetwork{{Slug: "a", PoolIps: 5, ObservedPoolIps: 5, ExclusiveShare: nan, SharedShare: nan,
				ExclusiveIps: nan, ObservedExclusiveIps: nan, Peers: []OverlapPeer{}, ran: ran}}, Market: OverlapMarket{SumOfPools: nan, SharedIps: nan, DistinctIps: nan,
				DuplicatedShare: nan, LikelyResellers: []string{}}}
			if err := a.listNA(); err != nil {
				t.Fatal(err)
			}
			n := a.Networks[0]
			if !slices.Equal(n.NA, []string{"exclusiveIps", "exclusiveScore", "exclusiveShare", "label", "observedExclusiveIps", "peers", "poolBasis", "sharedShare", "successScore"}) ||
				n.ReasonOf("peers") != want || n.ReasonOf("label") != want || a.ReasonOf("market.distinctIps") != NAInsufficient {
				t.Errorf("%s ran %v: na %v reason %s, market na %v", rangeID, ran, n.NA, n.ReasonOf("peers"), a.NA)
			}
		}
	}
}

// Every page answer added from Phase 3, on a production-sized build without
// catalog rows (as staging's): every null is listed in an na around it or
// null by contract, every listed path is null or empty, and the reasons are
// the catalog's, the window's and the cadence's.
func TestEveryPhase3AnswerListsItsNulls(t *testing.T) {
	t.Parallel()
	res := targetsScaleBuild(t, testscale.WeekInputs)
	pg := res.Pages
	bodies := map[string]any{}
	for k, a := range pg.Rankings {
		bodies["rankings "+k] = a
	}
	for k, a := range pg.RankingsByKind {
		bodies["rankings "+k] = a
	}
	for k, a := range pg.Network {
		bodies["network "+k] = a
	}
	for p, b := range pg.Registered {
		for _, family := range []string{"/v1/home/", "/v1/targets/", "/v1/overlap/", "/v1/meta"} {
			if strings.HasPrefix(p, family) {
				bodies[p] = b
			}
		}
	}
	// By design: the default targets body has no engine; a target the
	// browser score does not weigh has no weight (meta lists only a browser
	// target's).
	exempt := map[string]bool{"engine": true, "targets[].scoreWeight": true}
	for name, b := range bodies {
		for _, p := range naProblems(t, b, exempt) {
			t.Errorf("%s: %s", name, p)
		}
	}
	meta := pg.Registered["/v1/meta"].(MetaAnswer)
	reasons := []struct {
		of   *NAList
		path string
		want string
	}{
		{&pg.Rankings["24h"].NAList, "profiles", NACatalog},
		{&pg.Rankings["24h"].NAList, "cost.allTargets.cheapest", NACatalog},
		{&pg.Rankings["24h"].Networks[0].NAList, "listRate", NACatalog},
		{&pg.Rankings["24h"].Networks[0].NAList, "downtimeMinutes", NANoEvidence},
		{&pg.Rankings["24h"].Networks[0].NAList, "stickinessHoldRate", NANotMeasured},
		{&pg.Network["net00/residential"].NAList, "reviews", NACatalog},
		{&pg.Network["net00/residential"].Profile.NAList, "pricing.listRate", NACatalog},
		// Empty lists that mean "not collected" stay listed, as in the provider's na.
		{&pg.Network["net00/residential"].Profile.NAList, "ipSamples", NANotPublished},
		{&pg.Network["net00/residential"].Profile.NAList, "trust.blacklistHitRate", NANotMeasured},
		{&pg.Network["net00/residential"].Profile.NAList, "trust.stickinessHoldRate", NANotMeasured},
		{&pg.Network["net00/residential"].Profile.NAList, "trust.leakChecks", NANotMeasured},
		{&pg.Network["net00/residential"].Profile.NAList, "founded", NACatalog},
		{&pg.Network["net00/residential"].Stats["24h"].NAList, "claimShare", NACatalog},
		{&pg.Network["net00/residential"].Stats["24h"].NAList, "ranks.listRate.rank", NACatalog},
		{&pg.Registered["/v1/home/24h"].(*HomeAnswer).NAList, "profiles", NACatalog},
		{&pg.Registered["/v1/home/24h"].(*HomeAnswer).NAList, "market.cheapest", NACatalog},
		{&pg.Registered["/v1/targets/residential/24h"].(*TargetsAnswer).NAList, "targets[].difficulty", NACatalog},
		{&pg.Registered["/v1/targets/residential/24h"].(*TargetsAnswer).NAList, "networks[].cells.amazon_pdp.costPer1kSuccess", NACatalog},
		{&pg.Registered["/v1/overlap/residential/24h"].(*OverlapAnswer).Networks[0].NAList, "peers", NAInsufficient},
		{&meta.NAList, "profiles", NACatalog},
	}
	for _, r := range reasons {
		if got := r.of.ReasonOf(r.path); got != r.want {
			t.Errorf("%s: %q, want %s (na %v)", r.path, got, r.want, r.of.NA)
		}
	}
	// MEASURED-FIELDS-1: a network answer's every null has a reason code.
	ans := pg.Network["net00/residential"]
	lists := []*NAList{&ans.NAList, &ans.Profile.NAList}
	for _, s := range ans.Stats {
		lists = append(lists, &s.NAList)
	}
	for _, l := range lists {
		for _, p := range l.NA {
			if l.ReasonOf(p) == "" {
				t.Errorf("net00/residential: %s has no reason code", p)
			}
		}
	}
	// The headline latency is the daily tests' own (no load test), so the
	// home's market has a median and a fastest network.
	if m := pg.Registered["/v1/home/24h"].(*HomeAnswer).Market; !m.MedianLatency.OK() || m.Fastest == nil {
		t.Errorf("market latency %v fastest %v", m.MedianLatency, m.Fastest)
	}
	// A browser target's score status names every engine (TGT-3).
	for _, te := range pg.Registered["/v1/targets/residential/24h"].(*TargetsAnswer).Targets {
		if model.SV(te.Engine) == model.EngineBrowser && len(te.ScoreStatus) != len(TargetEngines) {
			t.Errorf("%s: score status %v", te.ID, te.ScoreStatus)
		}
	}
}

// naFrame is an object with an na around a value, and the value's path
// from it.
type naFrame struct {
	na   map[string]bool
	path string
}

// deeper is frames with each path one key (or []) deeper.
func deeper(frames []naFrame, key string) []naFrame {
	out := make([]naFrame, len(frames))
	for i, f := range frames {
		out[i] = naFrame{f.na, key}
		if f.path != "" && key == "[]" {
			out[i].path = f.path + key
		} else if f.path != "" {
			out[i].path = f.path + "." + key
		}
	}
	return out
}

// naProblems is every null of body that no na around it lists (by its path
// from that object, positions as []) and no contract or score status
// explains, and every listed path that reaches no null or empty value.
func naProblems(t *testing.T, body any, exempt map[string]bool) []string {
	t.Helper()
	g, err := decode(body)
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	var visit func(v any, frames []naFrame)
	visit = func(v any, frames []naFrame) {
		switch x := v.(type) {
		case nil:
			for _, f := range frames {
				if f.na[f.path] || byContract(f.path) || exempt[f.path] {
					return
				}
			}
			out = append(out, "null at "+frames[0].path+" not listed")
		case map[string]any:
			if na, ok := x["na"].([]any); ok {
				f := naFrame{na: map[string]bool{}}
				for _, p := range na {
					s := bracket.ReplaceAllString(p.(string), "[]")
					f.na[s] = true
					if !reachesNull(x, s) {
						out = append(out, s+" listed but has a value")
					}
				}
				frames = append(frames, f)
			}
			for k, child := range x {
				if k != "na" && k != "naReason" && k != "naReasons" {
					visit(child, deeper(frames, k))
				}
			}
		case []any:
			for _, child := range x {
				visit(child, deeper(frames, "[]"))
			}
		}
	}
	visit(g, []naFrame{{na: map[string]bool{}}})
	slices.Sort(out)
	return slices.Compact(out)
}

// naWithoutReason is every path of an na in body that has no reason: no
// naReasons entry and no naReason around it (#994).
func naWithoutReason(t *testing.T, body any) []string {
	t.Helper()
	g, err := decode(body)
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	var visit func(v any, at string)
	visit = func(v any, at string) {
		switch x := v.(type) {
		case map[string]any:
			na, _ := x["na"].([]any)
			reasons, _ := x["naReasons"].(map[string]any)
			for _, p := range na {
				if r, _ := reasons[p.(string)].(string); r == "" && x["naReason"] == nil {
					out = append(out, at+": "+p.(string)+" has no reason")
				}
			}
			for k, child := range x {
				visit(child, at+"."+k)
			}
		case []any:
			for i, child := range x {
				visit(child, at+"["+strconv.Itoa(i)+"]")
			}
		}
	}
	visit(g, "")
	slices.Sort(out)
	return out
}

// byContract reports whether a null is explained without an na: null by
// contract in its entity (a list element's path from it), or a score's
// field its status explains.
func byContract(p string) bool {
	if isScorePart(p) || strings.Contains(p, "scoreStatus.") && strings.HasSuffix(p, ".rankedOf") {
		return true
	}
	for {
		if contractNull[p] {
			return true
		}
		i := strings.Index(p, "[].")
		if i < 0 {
			return false
		}
		p = p[i+3:]
	}
}

// reachesNull reports whether path (keys and []) reaches a null, or an
// empty list or object, from v.
func reachesNull(v any, path string) bool {
	cur := []any{v}
	for _, key := range strings.Split(strings.ReplaceAll(path, "[]", ".[]"), ".") {
		var next []any
		for _, c := range cur {
			switch x := c.(type) {
			case nil:
				return true
			case []any:
				if len(x) == 0 {
					return true
				}
				if key == "[]" {
					next = append(next, x...)
				}
			case map[string]any:
				if len(x) == 0 {
					return true
				}
				if child, ok := x[key]; ok {
					next = append(next, child)
				}
			}
		}
		cur = next
	}
	for _, c := range cur {
		if c == nil || emptyValue(c) {
			return true
		}
	}
	return false
}
