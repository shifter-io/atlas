// Package registrytest registers throwaway page answers (api/answers/README.md)
// in a test binary of their own, so no other test's answers or goldens see them.
package registrytest

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/server"
	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// mode makes the throwaway answer build ("" ), fail ("fail") or give a body
// over the cap ("big"). The tests set it one at a time, never in parallel.
var mode string

func init() {
	build.RegisterAnswer(build.PageAnswer{
		Name:  "throwaway",
		Route: "GET /v1/throwaway",
		Key: func(r *http.Request) (string, error) {
			q, err := build.Query(r, "range")
			if err != nil {
				return "", err
			}
			rangeID, err := build.QueryRange(q)
			return "/v1/throwaway/" + rangeID, err
		},
		Build: func(c *build.Context) (map[string]any, error) {
			if mode == "fail" {
				return nil, errors.New("no rows")
			}
			out := map[string]any{}
			for _, r := range []string{"24h", "7d"} {
				body := map[string]any{"generatedAt": c.GeneratedAt, "range": r, "networks": len(c.Entries), "targets": len(c.Dataset.Targets)}
				if mode == "big" {
					body["pad"] = strings.Repeat("x", build.MaxAnswerBytes)
				}
				out["/v1/throwaway/"+r] = body
			}
			return out, nil
		},
		Samples: []string{"/v1/throwaway?range=7d"},
	})
	build.RegisterAnswer(build.PageAnswer{
		Name:  "throwaway-dataset",
		Route: "GET /v1/throwaway-dataset",
		Key:   func(*http.Request) (string, error) { return "/v1/throwaway-dataset", nil },
		Build: func(*build.Context) (map[string]any, error) {
			return map[string]any{"/v1/throwaway-dataset": []int{1}}, nil
		},
		Samples:   []string{"/v1/throwaway-dataset"},
		InDataset: true,
		Request:   func(path string) string { return path },
	})
	// A cut answer: the build holds the number of pages, a request cuts one.
	build.RegisterAnswer(build.PageAnswer{
		Name:  "throwaway-cut",
		Route: "GET /v1/throwaway-cut",
		Key: func(r *http.Request) (string, error) {
			q, err := build.Query(r, "page")
			if err != nil {
				return "", err
			}
			page, err := build.QueryPage(q)
			return fmt.Sprintf("/v1/throwaway-cut/%d", page), err
		},
		Build: func(*build.Context) (map[string]any, error) {
			return map[string]any{"/v1/throwaway-cut": 2}, nil
		},
		Cut: func(held map[string]any, path string) (any, bool) {
			cuts++
			page, _ := strconv.Atoi(strings.TrimPrefix(path, "/v1/throwaway-cut/"))
			return map[string]int{"page": page}, page <= held["/v1/throwaway-cut"].(int)
		},
		Samples: []string{"/v1/throwaway-cut?page=2"},
	})
}

// cuts counts the throwaway-cut answer's cuts.
var cuts int

// source is a two-network v4 build in memory.
type source struct{}

func (source) Fingerprint(context.Context) (store.Fingerprint, error) {
	return store.Fingerprint{SiteRevision: 1}, nil
}

func (source) Load(context.Context) (*store.Inputs, error) {
	return testscale.NetworkInputs(testscale.NetworkShape{Shape: testscale.Shape{Networks: 2, Countries: 1, Exits: 3, Tail: 4, CityAsn: 5}})
}

func (source) LoadEvents(context.Context) (*store.Inputs, error) {
	return &store.Inputs{EventsComplete: true, NotableComplete: true}, nil
}

func (source) Events(context.Context, store.EventFilter) ([]store.Event, error) { return nil, nil }

// built is a server over source, refreshed once, and the refresh's error.
func built(t *testing.T, m string) (*server.Server, error) {
	t.Helper()
	mode = m
	s := server.New(source{}, server.Options{FilesDir: t.TempDir()})
	_, err := s.Refresh(context.Background(), true)
	return s, err
}

func get(h http.Handler, path string, headers ...string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, path, nil)
	for i := 0; i+1 < len(headers); i += 2 {
		req.Header.Set(headers[i], headers[i+1])
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

// unserved is the admin /healthz's unserved pages of the throwaway answers, each as
// "<path> <code> <status>".
func unserved(t *testing.T, h http.Handler) []string {
	t.Helper()
	var hz server.Health
	if err := json.Unmarshal(get(h, "/healthz").Body.Bytes(), &hz); err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, u := range hz.Unserved {
		if line := fmt.Sprintf("%s %s %d", *u.Path, u.Code, *u.Status); strings.Contains(line, "throwaway") {
			out = append(out, line)
		}
	}
	return out
}

func TestARegisteredAnswerIsServedLikeEveryAnswer(t *testing.T) {
	s, err := built(t, "")
	if err != nil {
		t.Fatal(err)
	}
	h := s.Handler()
	rec := get(h, "/v1/throwaway?range=7d")
	var body struct {
		Range    string `json:"range"`
		Networks int    `json:"networks"`
	}
	if rec.Code != http.StatusOK || json.Unmarshal(rec.Body.Bytes(), &body) != nil || body.Range != "7d" || body.Networks != 2 {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	etag := rec.Header().Get("ETag")
	if etag == "" || !strings.Contains(rec.Header().Get("Cache-Control"), "max-age") || rec.Header().Get("X-Site-Version") == "" {
		t.Errorf("headers %v", rec.Header())
	}
	if r := get(h, "/v1/throwaway?range=7d", "If-None-Match", etag); r.Code != http.StatusNotModified {
		t.Errorf("If-None-Match: %d", r.Code)
	}
	if r := get(h, "/v1/throwaway?range=7d", "Accept-Encoding", "br"); r.Header().Get("Content-Encoding") != "br" {
		t.Errorf("brotli: %v", r.Header())
	}
	for path, want := range map[string]int{"/v1/throwaway?range=1y": 400, "/v1/throwaway?page=2": 400, "/v1/throwaway": 404} {
		if r := get(h, path); r.Code != want {
			t.Errorf("%s: %d %s, want %d", path, r.Code, r.Body, want)
		}
	}
	if lines := unserved(t, s.AdminHandler()); len(lines) != 0 {
		t.Errorf("unserved %v", lines)
	}
	// Only an answer with InDataset is in the dataset file's pages.
	pages, _ := s.Current().Result.Dataset.Pages.(build.DatasetPages)
	if _, ok := pages["/v1/throwaway-dataset"]; !ok || pages["/v1/throwaway/7d"] != nil {
		t.Errorf("dataset pages hold %v and %v", pages["/v1/throwaway-dataset"], pages["/v1/throwaway/7d"])
	}
}

// A cut answer's body is cut on the first request and kept: the second
// is served without a cut, with the same ETag; nothing is stored at build.
func TestACutAnswerIsCutOnRequestAndKept(t *testing.T) {
	s, err := built(t, "")
	if err != nil {
		t.Fatal(err)
	}
	h := s.Handler()
	before := cuts
	// Page 1 (page 2, the sample, was cut by the build's check of the samples).
	first, second := get(h, "/v1/throwaway-cut"), get(h, "/v1/throwaway-cut?page=1", "Accept-Encoding", "br")
	etag := strings.TrimSuffix(first.Header().Get("ETag"), `"`)
	if first.Code != http.StatusOK || first.Body.String() != `{"page":1}` || etag == "" || !strings.HasPrefix(second.Header().Get("ETag"), etag) ||
		second.Header().Get("Content-Encoding") != "br" || cuts != before+1 {
		t.Errorf("%d %s, then %v (%d cuts)", first.Code, first.Body, second.Header(), cuts-before)
	}
	for path, want := range map[string]int{"/v1/throwaway-cut?page=3": 404, "/v1/throwaway-cut?page=0": 400, "/v1/throwaway-cut?q=x": 400} {
		if r := get(h, path); r.Code != want {
			t.Errorf("%s: %d %s, want %d", path, r.Code, r.Body, want)
		}
	}
	for path := range s.Current().Static {
		if strings.HasPrefix(path, "/v1/throwaway-cut") {
			t.Errorf("%s was stored at build", path)
		}
	}
}

func TestAFailedRegisteredAnswerIsUnserved(t *testing.T) {
	s, err := built(t, "fail")
	if err != nil {
		t.Fatal(err)
	}
	h := s.Handler()
	if r := get(h, "/v1/throwaway-dataset"); r.Code != http.StatusOK {
		t.Errorf("the other answer: %d", r.Code)
	}
	lines := unserved(t, s.AdminHandler())
	if len(lines) != 1 || lines[0] != "/v1/throwaway?range=7d build_failed 404" {
		t.Fatalf("unserved %v", lines)
	}
}

func TestARegisteredAnswerOverTheCapFailsTheBuild(t *testing.T) {
	if _, err := built(t, "big"); err == nil || !strings.Contains(err.Error(), "answer /v1/throwaway/") || !strings.Contains(err.Error(), "above the") {
		t.Fatalf("refresh: %v", err)
	}
}

func TestAnAnswerIsRegisteredOnce(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Error("registered twice")
		}
	}()
	build.RegisterAnswer(build.PageAnswer{Name: "throwaway", Route: "GET /v1/throwaway", Key: func(*http.Request) (string, error) { return "", nil },
		Build: func(*build.Context) (map[string]any, error) { return nil, nil }})
}
