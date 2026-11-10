package bench

import (
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"os"
	"regexp"
	"slices"
	"strings"
	"time"
)

// Manifest is docs/api-web-map-gaps.json: every gap of docs/api-web-map.md
// and how it is closed (tests/test_api_web_map_gaps.py keeps it in step).
type Manifest struct {
	Gaps []Gap `json:"gaps"`
}

// Gap is one gap ID. An api gap has the work item that closes it and its
// checks; the other classes (website, owner, withdrawn) have a reason.
type Gap struct {
	ID     string  `json:"id"`
	Page   int     `json:"page"`
	Class  string  `json:"class"`
	Work   string  `json:"work,omitempty"`
	Issue  int     `json:"issue,omitempty"`
	Checks []Check `json:"checks,omitempty"`
	Reason string  `json:"reason,omitempty"`
}

// Check is a request template over {slug}, {kind}, {range} and {cc} and the
// JSON paths its answer must have. Paths use dots for keys, [] for every
// element and * for every key; a trailing ? marks a null by contract. Match
// holds a regexp that every value at a path must match.
type Check struct {
	Request string            `json:"request"`
	Paths   []string          `json:"paths"`
	Match   map[string]string `json:"match,omitempty"`
}

// LoadManifest reads and checks a manifest.
func LoadManifest(path string) (*Manifest, error) {
	body, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var m Manifest
	if err := json.Unmarshal(body, &m); err != nil {
		return nil, fmt.Errorf("gaps %s: %w", path, err)
	}
	for _, g := range m.Gaps {
		if g.Class == "api" && len(g.Checks) == 0 {
			return nil, fmt.Errorf("gaps %s: api gap %s has no check", path, g.ID)
		}
		for _, c := range g.Checks {
			if len(c.Paths) == 0 {
				return nil, fmt.Errorf("gaps %s: %s checks %s for no path", path, g.ID, c.Request)
			}
			for p, re := range c.Match {
				if _, err := regexp.Compile(re); err != nil {
					return nil, fmt.Errorf("gaps %s: %s match %s: %w", path, g.ID, p, err)
				}
			}
		}
	}
	return &m, nil
}

// GapReport is gaps.json, and gaps.md from Markdown.
type GapReport struct {
	BaseURL   string      `json:"baseUrl"`
	StartedAt time.Time   `json:"startedAt"`
	Revision  string      `json:"revision"`
	Passed    int         `json:"passed"`
	Failed    int         `json:"failed"`
	Listed    int         `json:"listed"`
	Gaps      []GapResult `json:"gaps"`
}

// GapResult is one gap's verdict (api gaps only: pass or fail) and, for a
// fail, the first failing request of each path.
type GapResult struct {
	Gap
	Verdict  string       `json:"verdict,omitempty"`
	Requests int          `json:"requests,omitempty"`
	Failures []GapFailure `json:"failures,omitempty"`
}

// GapFailure is a request and path that failed, and why.
type GapFailure struct {
	Request string `json:"request"`
	Path    string `json:"path,omitempty"`
	Why     string `json:"why"`
}

// CheckGaps requests every api gap's checks from the siteapi at base, for
// every network, kind and range.
func CheckGaps(ctx context.Context, base string, m *Manifest) (*GapReport, error) {
	base = strings.TrimRight(base, "/")
	c := newClient()
	r := &GapReport{BaseURL: base, StartedAt: time.Now().UTC()}
	var ver struct{ Revision string }
	if err := getJSON(ctx, c, base+"/v1/version", &ver); err != nil {
		return nil, err
	}
	r.Revision = ver.Revision
	sh, err := shape(ctx, c, base)
	if err != nil {
		return nil, err
	}
	answers := map[string]answer{}
	get := func(u string) answer {
		a, ok := answers[u]
		if !ok {
			a.err = getJSON(ctx, c, base+u, &a.body)
			answers[u] = a
		}
		return a
	}
	for _, g := range m.Gaps {
		res := GapResult{Gap: g}
		if g.Class == "api" {
			for _, chk := range g.Checks {
				requests := ExpandCheck(chk.Request, sh)
				res.Requests += len(requests)
				res.Failures = append(res.Failures, checkRequests(chk, requests, get)...)
			}
			res.Verdict = "pass"
			if len(res.Failures) > 0 {
				res.Verdict = "fail"
			}
		}
		switch res.Verdict {
		case "pass":
			r.Passed++
		case "fail":
			r.Failed++
		default:
			r.Listed++
		}
		r.Gaps = append(r.Gaps, res)
	}
	return r, nil
}

type answer struct {
	body any
	err  error
}

// checkRequests is the first failing request of each path of chk.
func checkRequests(chk Check, requests []string, get func(string) answer) []GapFailure {
	var out []GapFailure
	failed := map[string]bool{}
	for _, u := range requests {
		a := get(u)
		if a.err != nil {
			return append(out, GapFailure{Request: u, Why: a.err.Error()})
		}
		for _, p := range chk.Paths {
			if failed[p] {
				continue
			}
			why := checkPath(a.body, p)
			if why == "" && chk.Match[p] != "" {
				why = matchPath(a.body, p, chk.Match[p])
			}
			if why != "" {
				failed[p] = true
				out = append(out, GapFailure{Request: u, Path: p, Why: why})
			}
		}
	}
	if len(requests) == 0 {
		out = append(out, GapFailure{Request: chk.Request, Why: "expands to no request (no network or country)"})
	}
	return out
}

// ExpandCheck is a check's request for every network and kind ({slug},
// {kind}; {kind} alone: every kind once), every range and the country.
func ExpandCheck(request string, sh Shape) []string {
	if sh.Country == "" && strings.Contains(request, "{cc}") {
		return nil
	}
	slug, kind := strings.Contains(request, "{slug}"), strings.Contains(request, "{kind}")
	var out []string
	seen := map[string]bool{}
	for _, n := range sh.Networks {
		for _, k := range n.Kinds {
			key := "/"
			if slug {
				key = n.Slug + key
			}
			if kind {
				key += k
			}
			if !seen[key] {
				seen[key] = true
				out = appendNew(out, expandRoute(request, Ranges, map[string]string{"cc": sh.Country, "slug": n.Slug, "kind": k})...)
			}
		}
	}
	return out
}

// loc is one place a path reaches in an answer: the JSON value, its
// ancestors from the root, and the path to it in `na` notation.
type loc struct {
	value   any
	parents []any
	steps   []string
}

// checkPath is why path fails in body, or "". A path passes when it
// reaches a value at least once, and every null it reaches is null by
// contract (a trailing ?) or listed in the `na` of an object around it. A
// path that reaches nothing passes when it stops only at empty lists or
// objects, each listed in an `na` (no catalog profile, no review).
func checkPath(body any, path string) string {
	found := walk(body, path)
	for _, l := range found {
		if l.value == nil && !strings.HasSuffix(path, "?") && !l.inNA() {
			return "null at " + l.path(0) + ", not listed in na"
		}
	}
	if len(found) > 0 {
		return ""
	}
	ends := emptyEnds(body, path)
	for _, l := range ends {
		if !l.inNA() {
			return "missing (empty at " + l.path(0) + ", not listed in na)"
		}
	}
	if len(ends) == 0 {
		return "missing"
	}
	return ""
}

// segments is a path's keys, [] and * in order.
func segments(path string) []string {
	return strings.Split(strings.ReplaceAll(strings.TrimSuffix(path, "?"), "[]", ".[]"), ".")
}

// emptyEnds is every empty list ([]) or object (*) where path stops in v.
func emptyEnds(v any, path string) []loc {
	var out []loc
	cur := []loc{{value: v}}
	for _, seg := range segments(path) {
		var next []loc
		for _, l := range cur {
			if (seg == "[]" || seg == "*") && isEmpty(l.value) {
				out = append(out, l)
			} else if l.value != nil {
				next = append(next, l.children(seg)...)
			}
		}
		cur = next
	}
	return out
}

func isEmpty(v any) bool {
	switch x := v.(type) {
	case []any:
		return len(x) == 0
	case map[string]any:
		return len(x) == 0
	}
	return false
}

// matchPath is why a value at path does not match the regexp, or "".
func matchPath(body any, path, pattern string) string {
	re, err := regexp.Compile(pattern)
	if err != nil {
		return err.Error()
	}
	for _, l := range walk(body, path) {
		s, ok := l.value.(string)
		if !ok {
			b, _ := json.Marshal(l.value)
			s = string(b)
		}
		if l.value != nil && !re.MatchString(s) {
			return fmt.Sprintf("%s is %q, not %s", l.path(0), s, re)
		}
	}
	return ""
}

// walk is every place path reaches from v; a null ends a place early, and
// a missing key drops it.
func walk(v any, path string) []loc {
	out := []loc{{value: v}}
	for _, seg := range segments(path) {
		var next []loc
		for _, l := range out {
			if l.value == nil {
				next = append(next, l)
			} else {
				next = append(next, l.children(seg)...)
			}
		}
		out = next
	}
	return out
}

// children is where one segment leads from l: a key, every element ([]) or
// every key but na (*), in key order.
func (l loc) children(seg string) []loc {
	var out []loc
	switch x := l.value.(type) {
	case []any:
		for _, e := range x {
			if seg == "[]" {
				out = append(out, l.down(e, "[]"))
			}
		}
	case map[string]any:
		for _, k := range slices.Sorted(maps.Keys(x)) {
			if k == seg || seg == "*" && k != "na" {
				out = append(out, l.down(x[k], "."+k))
			}
		}
	}
	return out
}

func (l loc) down(v any, step string) loc {
	return loc{value: v, parents: append(slices.Clip(l.parents), l.value), steps: append(slices.Clip(l.steps), step)}
}

// path is the path to l from its i-th parent.
func (l loc) path(i int) string { return strings.TrimPrefix(strings.Join(l.steps[i:], ""), ".") }

var naBracket = regexp.MustCompile(`\[[^\]]*\]`)

// inNA reports whether a null is listed in the `na` of an object around it,
// by its path from that object (array positions and keys compared as []).
func (l loc) inNA() bool {
	for i := len(l.parents) - 1; i >= 0; i-- {
		m, ok := l.parents[i].(map[string]any)
		if !ok {
			continue
		}
		na, _ := m["na"].([]any)
		for _, p := range na {
			if s, ok := p.(string); ok && naBracket.ReplaceAllString(s, "[]") == l.path(i) {
				return true
			}
		}
	}
	return false
}

// Write writes gaps.json and gaps.md to dir (created 0700), each mode 600.
func (r *GapReport) Write(dir string) error {
	body, err := json.MarshalIndent(r, "", "  ")
	if err != nil {
		return err
	}
	return writeFiles(dir, map[string][]byte{"gaps.json": append(body, '\n'), "gaps.md": r.Markdown()})
}

// Markdown is gaps.md: the counts, then every gap with its verdict and
// evidence, or its reason.
func (r *GapReport) Markdown() []byte {
	var b strings.Builder
	fmt.Fprintf(&b, "# Map gaps against siteapi\n\n%s, %s, revision `%s`. %d pass, %d fail, %d listed without a verdict (website, owner, withdrawn).\n\n",
		r.BaseURL, r.StartedAt.Format(time.RFC3339), short(r.Revision), r.Passed, r.Failed, r.Listed)
	b.WriteString("| Gap | Page | Class | Work | Verdict | Evidence or reason |\n|---|---|---|---|---|---|\n")
	for _, g := range r.Gaps {
		work, why := "", strings.ReplaceAll(g.Reason, "|", `\|`)
		if g.Class == "api" {
			work = fmt.Sprintf("%s (#%d)", g.Work, g.Issue)
			why = fmt.Sprintf("%d requests", g.Requests)
		}
		for _, f := range g.Failures {
			why += fmt.Sprintf("<br>`%s` %s: %s", f.Request, f.Path, strings.ReplaceAll(f.Why, "|", `\|`))
		}
		fmt.Fprintf(&b, "| %s | %d | %s | %s | %s | %s |\n", g.ID, g.Page, g.Class, work, g.Verdict, why)
	}
	return []byte(b.String())
}
