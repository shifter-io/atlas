package server

import (
	"encoding/json"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// The time scan (CONSISTENCY-1, #982): every time in every answer, list
// page, events answer and the dataset file is RFC 3339 in UTC with Z and at
// most milliseconds. Date-only values (checkedAt, invoiceMonth) keep their
// date form.

// timeLike is a value that starts with a date and a time of day.
var timeLike = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}`)

// timeForm is the one form a time takes.
var timeForm = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$`)

// timesOf lists, in v (a body's generic JSON), the path of every time value
// into seen and of every one in another form, with an example, into bad.
func timesOf(v any, path, parent string, seen map[string]bool, bad map[string]string) {
	switch x := v.(type) {
	case map[string]any:
		for k, child := range x {
			timesOf(child, path+"."+label(k, parent), k, seen, bad)
		}
	case []any:
		for _, child := range x {
			timesOf(child, path+"[]", parent+"[]", seen, bad)
		}
	case string:
		if !timeLike.MatchString(x) {
			return
		}
		p := normalize(path)
		seen[p] = true
		if !timeForm.MatchString(x) {
			bad[p] = x
		}
	}
}

// scanTimes is every path of a time in another form in bodies, sorted with
// an example each, and every path of a time.
func scanTimes(bodies map[string][]byte) (found []string, seen map[string]bool) {
	seen, bad := map[string]bool{}, map[string]string{}
	for name, b := range bodies {
		var doc any
		if json.Unmarshal(b, &doc) != nil {
			continue // not a JSON body
		}
		timesOf(doc, bodyGroup(name), "", seen, bad)
	}
	for p, x := range bad {
		found = append(found, p+" "+x)
	}
	sort.Strings(found)
	return found, seen
}

// TestEveryTimeHasOneForm walks every answer, a page of every long list,
// the events answer, the health answer and the dataset file of the scan
// build, and fails on any time in another form.
func TestEveryTimeHasOneForm(t *testing.T) {
	t.Parallel()
	bodies, srv := scanServer(t)
	h := srv.Handler()
	for _, p := range []string{"/v1/events", "/v1/events?limit=5", "/healthz"} {
		bodies[p] = get(h, p).Body.Bytes()
	}
	found, seen := scanTimes(bodies)
	if len(found) > 0 {
		t.Errorf("%d times in another form:\n%s", len(found), strings.Join(found, "\n"))
	}
	for _, want := range []string{"/v1/meta.generatedAt", "dataset file.generatedAt", "/v1/events.events[].at", "/v1/events.updatedAt",
		"/v1/events.events[].data.since", "/v1/version.builtAt", "/healthz.builtAt"} {
		if !seen[want] {
			t.Errorf("no body serves a time at %s", want)
		}
	}
}

func TestTimeScanFindsOtherForms(t *testing.T) {
	t.Parallel()
	doc := `{"a":"2026-11-11T14:09:42.371728+00:00","b":"2026-11-09T04:02:32.000Z","c":"2026-11-09T04:02:32Z",
		"d":["2026-11-09T04:02:32.123456789Z"],"e":"2026-11-09","f":"2026-11-09 04:02:32","g":"net-2026-11-09T04:02:32+00:00"}`
	found, seen := scanTimes(map[string][]byte{"/v1/x": []byte(doc)})
	want := []string{"/v1/x.a 2026-11-11T14:09:42.371728+00:00", "/v1/x.d[] 2026-11-09T04:02:32.123456789Z", "/v1/x.f 2026-11-09 04:02:32"}
	if strings.Join(found, ",") != strings.Join(want, ",") {
		t.Errorf("found %v, want %v", found, want)
	}
	if !seen["/v1/x.b"] || !seen["/v1/x.c"] || seen["/v1/x.e"] || seen["/v1/x.g"] {
		t.Errorf("seen %v", seen)
	}
}
