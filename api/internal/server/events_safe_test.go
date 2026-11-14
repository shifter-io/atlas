package server

import (
	"encoding/json"
	"fmt"
	"net/url"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// /v1/events input is bounded (SEC-EVENTS-SAFE-1): kind= and event= take at
// most 32 event codes (kind= also groups), product= and target= at most 100
// bytes; anything else answers 400 with a typed error.
func TestEventsInputIsBounded(t *testing.T) {
	t.Parallel()
	many := strings.TrimSuffix(strings.Repeat("down,", maxEventCodes+1), ",")
	long := strings.Repeat("a", maxEventText+1)
	s := newTestServer(t, &fakeSource{rev: 1, events: testscale.TapeEvents(3), complete: true})
	for query, want := range map[string]string{
		"kind=" + many:        `{"code":"too_many_values","params":{"max":32,"name":"kind"}}`,
		"event=" + many:       `{"code":"too_many_values","params":{"max":32,"name":"event"}}`,
		"kind=nope":           `"name":"kind","value":"nope"`,
		"kind=down,nope":      `"name":"kind","value":"nope"`,
		"event=census":        `"name":"event","value":"census"`,
		"product=" + long:     `{"code":"too_long","params":{"max":100,"name":"product"}}`,
		"target=" + long:      `{"code":"too_long","params":{"max":100,"name":"target"}}`,
		"kind=" + many[:5*32]: "",
		"product=" + long[1:]: "",
		"target=" + long[1:]:  "",
	} {
		r := get(s.Handler(), "/v1/events?"+query)
		switch {
		case want == "" && r.Code != 200:
			t.Errorf("%.40s: %d %s", query, r.Code, r.Body.String())
		case want != "" && (r.Code != 400 || !strings.Contains(r.Body.String(), want)):
			t.Errorf("%.40s: %d %s, want 400 with %s", query, r.Code, r.Body.String(), want)
		}
	}
	// kind= reads a code as its raw kind, like event=.
	q, _ := url.ParseQuery("kind=hour_released,estimate_hour_released,census")
	if f, err := ParseEventFilter(q, nil); err != nil || !slices.Equal(f.Kinds, []string{"estimate_hour_released", "estimate_hour_released"}) {
		t.Fatalf("kind codes: %+v %v", f, err)
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

// A /v1/events row serves only its kind's registered data keys: a key no
// producer registered, a machine name or free error text is never public.
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
		Events []struct {
			Kind   string          `json:"kind"`
			Data   json.RawMessage `json:"data"`
			Values map[string]any  `json:"values"`
		} `json:"events"`
	}
	if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &b) != nil || len(b.Events) != 3 {
		t.Fatalf("%d %s", r.Code, r.Body.String())
	}
	want := map[string]string{"node_offline": `{"region":"fra1","provider":"net00"}`,
		"census_finished": `{"status":"failed","requests":10}`, "some_future_kind": `{}`}
	for _, e := range b.Events {
		if string(e.Data) != want[e.Kind] {
			t.Errorf("%s data %s, want %s", e.Kind, e.Data, want[e.Kind])
		}
	}
	if keys := len(b.Events[2].Values); keys != 8 {
		t.Errorf("an unregistered kind's values: %v", b.Events[2].Values)
	}
}
