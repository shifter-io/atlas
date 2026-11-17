package server

import (
	"encoding/json"
	"fmt"
	"maps"
	"net/url"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

// /v1/events input is bounded (SEC-EVENTS-SAFE-1): kind= takes at most 32
// groups and event= at most 32 event codes (#993: a code in kind= and
// category= answer 400), product= and target= at most 100 bytes; anything
// else answers 400 with a typed error.
func TestEventsInputIsBounded(t *testing.T) {
	t.Parallel()
	many := strings.TrimSuffix(strings.Repeat("down,", maxEventCodes+1), ",")
	groups := strings.TrimSuffix(strings.Repeat("audit,", maxEventCodes+1), ",")
	long := strings.Repeat("a", maxEventText+1)
	s := newTestServer(t, &fakeSource{rev: 1, events: testscale.TapeEvents(3), complete: true})
	for query, want := range map[string]string{
		"kind=" + groups:        `{"code":"too_many_values","params":{"max":32,"name":"kind"}}`,
		"event=" + many:         `{"code":"too_many_values","params":{"max":32,"name":"event"}}`,
		"kind=nope":             `"name":"kind","value":"nope"`,
		"kind=audit,nope":       `"name":"kind","value":"nope"`,
		"kind=down":             `{"code":"invalid_parameter","params":{"allowed":["census","pulse","incident","audit"],"name":"kind","value":"down"}}`,
		"kind=hour_released":    `"name":"kind","value":"hour_released"`,
		"category=pulse":        `{"code":"unknown_parameter","params":{"name":"category"}}`,
		"event=census":          `"name":"event","value":"census"`,
		"product=" + long:       `{"code":"too_long","params":{"max":100,"name":"product"}}`,
		"target=" + long:        `{"code":"too_long","params":{"max":100,"name":"target"}}`,
		"kind=" + groups[:6*32]: "",
		"event=" + many[:5*32]:  "",
		"product=" + long[1:]:   "",
		"target=" + long[1:]:    "",
	} {
		r := get(s.Handler(), "/v1/events?"+query)
		switch {
		case want == "" && r.Code != 200:
			t.Errorf("%.40s: %d %s", query, r.Code, r.Body.String())
		case want != "" && (r.Code != 400 || !strings.Contains(r.Body.String(), want)):
			t.Errorf("%.40s: %d %s, want 400 with %s", query, r.Code, r.Body.String(), want)
		}
	}
	// event= reads a code as its raw kind.
	q, _ := url.ParseQuery("event=hour_released,estimate_hour_released&kind=census")
	if f, err := ParseEventFilter(q, nil); err != nil || !slices.Equal(f.Events, []string{"estimate_hour_released", "estimate_hour_released"}) ||
		!slices.Equal(f.Groups, []string{"census"}) {
		t.Fatalf("event codes: %+v %v", f, err)
	}
}

// 1,000 unknown kinds answer 400 at once: the list is refused before any
// value is looked at.
func TestManyUnknownKindsAnswerFast(t *testing.T) {
	t.Parallel()
	s := newTestServer(t, &fakeSource{rev: 1, events: testscale.TapeEvents(3), complete: true})
	kinds := make([]string, 1000)
	for i := range kinds {
		kinds[i] = fmt.Sprintf("unknown_kind_%d", i)
	}
	path := "/v1/events?kind=" + strings.Join(kinds, ",")
	best := time.Hour
	for range 5 { // the best of 5, so a busy runner does not fail it
		start := time.Now()
		r := get(s.Handler(), path)
		best = min(best, time.Since(start))
		if r.Code != 400 {
			t.Fatalf("%d %s", r.Code, r.Body.String())
		}
	}
	if best > 5*time.Millisecond {
		t.Fatalf("1,000 unknown kinds took %v, want under 5 ms", best)
	}
}

// A /v1/events row has the tape row's fields only (#993): no stored data, no
// category and no framework provider, so a key no producer registered, a
// machine name or free error text is never public; its values hold only its
// kind's registered keys.
func TestEventsServeRegisteredDataKeys(t *testing.T) {
	t.Parallel()
	at := time.Date(2026, 11, 13, 10, 0, 0, 0, time.UTC)
	provider := "net00"
	src := &fakeSource{rev: 1, complete: true, events: []store.Event{
		{ID: 3, At: at, Kind: "node_offline", Provider: &provider, Severity: "major",
			Data: json.RawMessage(`{"host": "worker-7", "region": "fra1", "node_id": 7, "provider": "net00"}`)},
		{ID: 2, At: at.Add(-time.Minute), Kind: "census_finished", Provider: &provider, Severity: "info",
			Data: json.RawMessage(`{"error": "connection refused", "status": "failed", "requests": 10, "new_key": 1}`)},
		{ID: 1, At: at.Add(-2 * time.Minute), Kind: "some_future_kind", Provider: &provider, Severity: "info",
			Data: json.RawMessage(`{"region": "fra1"}`)},
	}}
	r := get(newTestServer(t, src).Handler(), "/v1/events")
	var b struct {
		Events []map[string]json.RawMessage `json:"events"`
	}
	if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &b) != nil || len(b.Events) != 3 {
		t.Fatalf("%d %s", r.Code, r.Body.String())
	}
	for _, leak := range []string{"worker-7", "node_id", "connection refused", "new_key", `"data"`, `"category"`, `"provider"`} {
		if strings.Contains(r.Body.String(), leak) {
			t.Errorf("the answer has %s: %s", leak, r.Body.String())
		}
	}
	keys := "at country event id kind product providerName providerSlug region severity signal target targets values"
	for _, e := range b.Events {
		if got := strings.Join(slices.Sorted(maps.Keys(e)), " "); got != keys {
			t.Errorf("row keys %s, want %s", got, keys)
		}
	}
	var values map[string]any
	json.Unmarshal(b.Events[2]["values"], &values) //nolint:errcheck
	if len(values) != 8 {
		t.Errorf("an unregistered kind's values: %v", values)
	}
}
