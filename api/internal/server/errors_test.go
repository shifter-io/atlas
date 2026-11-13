package server

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// failingEvents is a source whose events query fails.
type failingEvents struct{ *fakeSource }

func (failingEvents) Events(context.Context, store.EventFilter) ([]store.Event, error) {
	return nil, errors.New("database unreachable")
}

// wantError checks rec is status with body {"error": want}, uncached, and
// that its code and its format and reason are registered (codes_errors.go).
func wantError(t *testing.T, name string, rec *httptest.ResponseRecorder, status int, want string) {
	t.Helper()
	if got := strings.TrimSpace(rec.Body.String()); rec.Code != status || got != `{"error":`+want+`}` {
		t.Errorf("%s: %d %s, want %d %s", name, rec.Code, got, status, want)
	}
	if h := rec.Header(); h.Get("Cache-Control") != CacheNone || h.Get("Content-Type") != "application/json; charset=utf-8" {
		t.Errorf("%s: headers %v", name, h)
	}
	var body struct{ Error build.APIError }
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	registered := map[string]bool{}
	for _, s := range build.CodeSets() {
		for _, c := range s.Values {
			registered[s.Name+" "+c.Code] = true
		}
	}
	for set, code := range map[string]any{"error": body.Error.Code, "error_format": body.Error.Params["format"],
		"unavailable_reason": body.Error.Params["reason"]} {
		if s, ok := code.(string); ok && !registered[set+" "+s] {
			t.Errorf("%s: %q is no %s code", name, s, set)
		}
	}
}

func TestEveryErrorIsACodeWithParams(t *testing.T) {
	t.Parallel()
	h := newTestServer(t, &fakeSource{rev: 1}).Handler()
	long := strings.Repeat("a", 201)
	for path, want := range map[string]string{
		"/v1/nope":                                    `{"code":"not_found","params":{"path":"/v1/nope"}}`,
		"/v1/networks/nope":                           `{"code":"not_found","params":{"path":"/v1/networks/nope"}}`,
		"/v1/files/nope.json":                         `{"code":"not_found","params":{"path":"/v1/files/nope.json"}}`,
		"/v1/home?zz=1&foo=1&yy=2":                    `{"code":"unknown_parameter","params":{"name":"foo"}}`,
		"/v1/search?page=2":                           `{"code":"unknown_parameter","params":{"name":"page"}}`,
		"/v1/events?offset=5&bad=1":                   `{"code":"unknown_parameter","params":{"name":"bad"}}`,
		"/v1/rankings?range=99d":                      `{"code":"invalid_parameter","params":{"allowed":["24h","7d","30d"],"name":"range","value":"99d"}}`,
		"/v1/rankings?kind=hosting":                   `{"code":"invalid_parameter","params":{"allowed":["residential","datacenter","mobile","isp"],"name":"kind","value":"hosting"}}`,
		"/v1/networks/Bad_Slug":                       `{"code":"invalid_parameter","params":{"format":"slug","name":"slug","value":"Bad_Slug"}}`,
		"/v1/networks/alpha-net/chart?range=1y":       `{"code":"invalid_parameter","params":{"allowed":["24h","7d","30d"],"name":"range","value":"1y"}}`,
		"/v1/networks/alpha-net/places?country=USA":   `{"code":"invalid_parameter","params":{"format":"country","name":"country","value":"USA"}}`,
		"/v1/countries/USA":                           `{"code":"invalid_parameter","params":{"format":"country","name":"cc","value":"USA"}}`,
		"/v1/places/street/x":                         `{"code":"invalid_parameter","params":{"allowed":["region","city"],"name":"dimension","value":"street"}}`,
		"/v1/events?category=x":                       `{"code":"invalid_parameter","params":{"allowed":["census","pulse","incident","audit"],"name":"category","value":"x"}}`,
		"/v1/events?network=nope":                     `{"code":"invalid_parameter","params":{"name":"network","value":"nope"}}`,
		"/v1/events?country=u1":                       `{"code":"invalid_parameter","params":{"format":"country","name":"country","value":"u1"}}`,
		"/v1/events?since=yesterday":                  `{"code":"invalid_parameter","params":{"format":"iso_time","name":"since","value":"yesterday"}}`,
		"/v1/events?before=x":                         `{"code":"invalid_parameter","params":{"format":"event_id","name":"before","value":"x"}}`,
		"/v1/events?limit=x":                          `{"code":"invalid_parameter","params":{"format":"positive_integer","name":"limit","value":"x"}}`,
		"/v1/events?limit=9999":                       `{"code":"out_of_range","params":{"max":500,"min":1,"name":"limit","value":9999}}`,
		"/v1/networks/alpha-net/places?page=0":        `{"code":"out_of_range","params":{"max":100000,"min":1,"name":"page","value":0}}`,
		"/v1/search?q=" + long:                        `{"code":"too_long","params":{"max":200,"name":"q"}}`,
		"/v1/events?q=" + long:                        `{"code":"too_long","params":{"max":200,"name":"q"}}`,
		"/v1/places/city/" + strings.Repeat("a", 401): `{"code":"too_long","params":{"max":400,"name":"key"}}`,
		"/v1/events?before=999":                       `{"code":"unknown_cursor","params":{"name":"before","value":"999"}}`,
		"/v1/events?before=1&after=2":                 `{"code":"conflicting_parameters","params":{"names":["before","after"]}}`,
	} {
		status := http.StatusBadRequest
		if strings.Contains(want, "not_found") {
			status = http.StatusNotFound
		}
		wantError(t, path, get(h, path), status, want)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/networks", nil))
	wantError(t, "POST", rec, http.StatusMethodNotAllowed, `{"code":"method_not_allowed","params":{"method":"POST"}}`)
}

func TestUnavailableAnswersNameTheirReason(t *testing.T) {
	t.Parallel()
	starting := New(&fakeSource{}, Options{}).Handler()
	wantError(t, "starting", get(starting, "/v1/networks"), http.StatusServiceUnavailable,
		`{"code":"unavailable","params":{"reason":"cache_not_built"}}`)
	before := New(&fakeSource{}, Options{})
	before.state.reason = BeforeV4Reason
	wantError(t, "before v4", get(before.Handler(), "/v1/home"), http.StatusServiceUnavailable,
		`{"code":"unavailable","params":{"reason":"payload_before_v4"}}`)
	events := newTestServer(t, failingEvents{&fakeSource{rev: 1}}).Handler()
	wantError(t, "events", get(events, "/v1/events?before=7"), http.StatusServiceUnavailable,
		`{"code":"unavailable","params":{"reason":"events_query_failed"}}`)
	s := newTestServer(t, &fakeSource{rev: 1})
	v := s.Current()
	if err := os.RemoveAll(v.Dataset.dir); err != nil {
		t.Fatal(err)
	}
	wantError(t, "dataset file", get(s.Handler(), v.DatasetPath), http.StatusServiceUnavailable,
		`{"code":"unavailable","params":{"reason":"dataset_file_missing"}}`)
	rec := httptest.NewRecorder()
	s.renderFailed(rec, httptest.NewRequest(http.MethodGet, "/v1/networks/x/places", nil), errors.New("rows unreadable"))
	wantError(t, "internal", rec, http.StatusInternalServerError, `{"code":"internal_error","params":{}}`)
}

// /healthz's reason is a code, its fingerprint an object, and its unserved
// pages objects with the count beyond them.
func TestHealthHoldsCodesAndObjects(t *testing.T) {
	t.Parallel()
	before := New(&fakeSource{}, Options{})
	before.state.reason = BeforeV4Reason
	var hz map[string]any
	if err := json.Unmarshal(get(before.Handler(), "/healthz").Body.Bytes(), &hz); err != nil || hz["reason"] != "payload_before_v4" ||
		hz["fingerprint"] != nil {
		t.Fatalf("before v4: %v %v", hz, err)
	}
	s := newTestServer(t, &fakeSource{rev: 1})
	path, status, product := "/v1/networks/x", 404, "x:residential"
	s.state.unserved, s.state.unservedMore = []Unserved{{Path: &path, Code: UnservedNotFound, Status: &status, Product: &product}}, 3
	body := get(s.Handler(), "/healthz").Body.Bytes()
	if err := json.Unmarshal(body, &hz); err != nil {
		t.Fatal(err)
	}
	fp, _ := hz["fingerprint"].(map[string]any)
	if fp["site_revision"] != 1.0 || hz["reason"] != nil || hz["unservedMore"] != 3.0 ||
		!strings.Contains(string(body), `"unserved":[{"path":"/v1/networks/x","code":"not_found","status":404,"product":"x:residential"}]`) {
		t.Fatalf("serving: %s", body)
	}
}
