package server

import (
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
)

// The codes scan (CODES-CATALOG-1): every value at a registered code set's
// field paths (build.CodeSet.UsedIn) is null or a code of that set, the
// catalog sets' codes as /v1/codes serves them.

// codesUnscanned are parts of UsedIn entries the scan build holds no value
// at, with why. Each must stand for an entry the scan does not reach, and
// the scan fails on one that stands for none.
var codesUnscanned = map[string]string{
	"/healthz ":            "the health answer is no page body",
	"/v1/events events[]":  "/v1/events is served on request; the tapes carry its codes",
	"reviews":              "the scan build has no review",
	"changelog entries[].": "the scan build has no change in any range",
	"changelog[].":         "the scan build has no change in any range",
	"pricing.plans[]":      "the scan build has no catalog plan",
	"profiles[].":          "the scan build has no ranking profile row",
}

// bodyRoutes name a scanned body's route by its stored path or request: the
// first pattern that matches replaces the part it matches; any other body's
// route is its path without the query.
var bodyRoutes = []struct {
	pattern *regexp.Regexp
	route   string
}{
	{regexp.MustCompile(`^/v1/charts/.*`), "/v1/networks/{slug}/chart"},
	{regexp.MustCompile(`^/v1/networks/[^/?]+/(runs|places|reviews)([/?].*)?$`), "/v1/networks/{slug}/$1"},
	{regexp.MustCompile(`^/v1/networks/[^/?]+(/.*)?$`), "/v1/networks/{slug}"},
	{regexp.MustCompile(`^/v1/countries/[^/?]+$`), "/v1/countries/{cc}"},
	{regexp.MustCompile(`^/v1/coverage/[^/?]+$`), "/v1/coverage/{slug}"},
	{regexp.MustCompile(`^/v1/events/stats.*`), "/v1/events/stats"},
	{regexp.MustCompile(`^/v1/(home|targets|rankings|overlap|changelog)[/?].*`), "/v1/$1"},
	{regexp.MustCompile(`^dataset file$`), "/v1/dataset"},
}

func bodyRoute(name string) string {
	for _, r := range bodyRoutes {
		if r.pattern.MatchString(name) {
			return r.pattern.ReplaceAllString(name, r.route)
		}
	}
	path, _, _ := strings.Cut(name, "?")
	return path
}

func TestEveryServedCodeIsRegistered(t *testing.T) {
	t.Parallel()
	bodies := scanBodies(t)
	var served build.CodesAnswer
	if err := json.Unmarshal(bodies["/v1/codes"], &served); err != nil {
		t.Fatalf("/v1/codes: %v", err)
	}
	seen, bad := map[string]bool{}, map[string]bool{}
	for name, b := range bodies {
		var doc any
		if json.Unmarshal(b, &doc) != nil {
			continue // not a JSON body
		}
		for _, s := range served.Sets {
			scanCodes(doc, name, s, seen, bad)
		}
	}
	used := map[string]bool{}
	for _, s := range served.Sets {
		if s.Source == build.CodesFromCatalog && len(s.Values) == 0 {
			t.Errorf("catalog set %s has no code in the scan build", s.Name)
		}
		for _, u := range s.UsedIn {
			if part := unscannedPart(u); seen[u] && part != "" {
				t.Errorf("code set %s: %s is scanned now; %q (%s) stands for it", s.Name, u, part, codesUnscanned[part])
			} else if !seen[u] && part == "" {
				t.Errorf("code set %s: no body holds %s", s.Name, u)
			} else if !seen[u] {
				used[part] = true
			}
		}
	}
	for part, why := range codesUnscanned {
		if !used[part] {
			t.Errorf("codesUnscanned %q (%s) stands for no entry: remove it", part, why)
		}
	}
	if len(bad) > 0 {
		t.Errorf("%d unregistered codes:\n%s", len(bad), strings.Join(sorted(bad), "\n"))
	}
}

// unscannedPart is the codesUnscanned part entry u holds, "" for none.
func unscannedPart(u string) string {
	for part := range codesUnscanned {
		if strings.Contains(u, part) {
			return part
		}
	}
	return ""
}

// scanCodes checks the values of body name (doc) at each of s's paths.
func scanCodes(doc any, name string, s build.CodeSet, seen, bad map[string]bool) {
	allowed := map[string]bool{}
	for _, c := range s.Values {
		allowed[c.Code] = true
	}
	for _, u := range s.UsedIn {
		route, path, _ := strings.Cut(u, " ")
		if route != "*" && route != bodyRoute(name) {
			continue
		}
		walkCodes(doc, strings.Split(path, "."), func(v any) {
			seen[u] = true
			if code, ok := v.(string); v != nil && (!ok || !allowed[code]) {
				bad[fmt.Sprintf("%s: %s %s = %v", s.Name, bodyGroup(name), path, v)] = true
			}
		})
	}
}

// walkCodes calls yield with every value of v at path segs: a name, <id>
// (any key) or <key> (the keys themselves, last), each with a [] per list
// level, or ** (any keys and list positions, none included, never into a
// naReasons, whose keys are paths, or the N/A report's sections, whose keys
// are section names). A null a key holds on the way is the value.
func walkCodes(v any, segs []string, yield func(any)) {
	if len(segs) == 0 {
		yield(v)
		return
	}
	if segs[0] == "**" {
		walkCodes(v, segs[1:], yield)
		forChildren(v, func(k string, c any) {
			if _, sections := c.(map[string]any); k != "naReasons" && (k != "sections" || !sections) {
				walkCodes(c, segs, yield)
			}
		})
		return
	}
	m, ok := v.(map[string]any)
	name, lists := strings.TrimRight(segs[0], "[]"), strings.Count(segs[0], "[]")
	switch {
	case !ok:
	case name == "<key>":
		for k := range m {
			yield(k)
		}
	case name == "<id>":
		for _, c := range m {
			walkLists(c, lists, segs[1:], yield)
		}
	default:
		if c, ok := m[name]; ok {
			walkLists(c, lists, segs[1:], yield)
		}
	}
}

func walkLists(v any, lists int, rest []string, yield func(any)) {
	l, isList := v.([]any)
	switch {
	case v == nil:
		yield(nil)
	case lists == 0:
		walkCodes(v, rest, yield)
	case isList:
		for _, c := range l {
			walkLists(c, lists-1, rest, yield)
		}
	}
}

func forChildren(v any, fn func(string, any)) {
	switch x := v.(type) {
	case map[string]any:
		for k, c := range x {
			fn(k, c)
		}
	case []any:
		for _, c := range x {
			fn("", c)
		}
	}
}

func sorted(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for k := range set {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
