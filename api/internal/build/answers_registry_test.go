package build

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// RegisterAnswer refuses a bad, reserved or incomplete answer before it adds
// anything, so these cases leave the registry as it was.
func TestRegisterAnswerRefusesABadAnswer(t *testing.T) {
	t.Parallel()
	before := Answers()
	ok := PageAnswer{Name: "fine", Route: "GET /v1/fine", Build: func(*Context) (map[string]any, error) { return nil, nil },
		Key: func(*http.Request) (string, error) { return "/v1/fine", nil }}
	for name, edit := range map[string]func(*PageAnswer){
		"reserved":                  func(a *PageAnswer) { a.Name = "networks" },
		"bad name":                  func(a *PageAnswer) { a.Name = "Fine/1" },
		"no build":                  func(a *PageAnswer) { a.Build = nil },
		"no key":                    func(a *PageAnswer) { a.Key = nil },
		"not a GET":                 func(a *PageAnswer) { a.Route = "POST /v1/fine" },
		"dataset without a request": func(a *PageAnswer) { a.InDataset = true },
	} {
		a := ok
		edit(&a)
		func() {
			defer func() {
				if recover() == nil {
					t.Errorf("%s: registered", name)
				}
			}()
			RegisterAnswer(a)
		}()
	}
	if got := Answers(); len(got) != len(before) {
		t.Fatalf("registered %v", got)
	}
}

func TestAnAnswerOwnsItsFamilyOnly(t *testing.T) {
	t.Parallel()
	a := PageAnswer{Name: "targets"}
	for path, want := range map[string]bool{"/v1/targets": true, "/v1/targets/residential/7d": true, "/v1/targets-x": false,
		"/v1/networks/targets": false, "/v1/target": false} {
		if a.Owns(path) != want {
			t.Errorf("Owns(%s) = %v", path, !want)
		}
	}
}

// buildRegistered keeps a good answer's bodies and leaves out a failed one
// and one with a path outside its family, noting why.
func TestBuildRegisteredLeavesOutAFailedAnswer(t *testing.T) {
	t.Parallel()
	pg := &Pages{}
	pg.buildFrom(&Context{GeneratedAt: "now"}, []PageAnswer{
		{Name: "good", Build: func(c *Context) (map[string]any, error) {
			return map[string]any{"/v1/good/7d": c.GeneratedAt, "/v1/good": 1}, nil
		}},
		{Name: "failed", Build: func(*Context) (map[string]any, error) { return nil, errors.New("no rows") }},
		{Name: "outside", Build: func(*Context) (map[string]any, error) { return map[string]any{"/v1/networks": 1}, nil }},
		{Name: "held", Build: func(*Context) (map[string]any, error) { return map[string]any{"/v1/held/x": 2}, nil },
			Cut: func(map[string]any, string) (any, bool) { return nil, false }},
	})
	if pg.Held["held"]["/v1/held/x"] != 2 || len(pg.Held) != 1 {
		t.Errorf("held %v", pg.Held)
	}
	if len(pg.Registered) != 2 || pg.Registered["/v1/good/7d"] != "now" {
		t.Errorf("registered %v", pg.Registered)
	}
	if pg.Unbuilt["failed"] != "no rows" || !strings.Contains(pg.Unbuilt["outside"], "/v1/networks is outside /v1/outside") || len(pg.Unbuilt) != 2 {
		t.Errorf("unbuilt %v", pg.Unbuilt)
	}
}

// queryRequest spells a stored path's segments out as the query, and gives
// "" for a path with more segments than names.
func TestQueryRequestSpellsThePathOut(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		path, root, want string
		names            []string
	}{
		{"/v1/meta", "/v1/meta", "/v1/meta", nil},
		{"/v1/home/7d", "/v1/home", "/v1/home?range=7d", []string{"range"}},
		{"/v1/targets/isp/24h/chrome", "/v1/targets", "/v1/targets?kind=isp&range=24h&engine=chrome", []string{"kind", "range", "engine"}},
		{"/v1/events/stats/7d/net00", "/v1/events/stats", "", []string{"range"}},
	} {
		if got := queryRequest(c.path, c.root, c.names...); got != c.want {
			t.Errorf("%s: %q, want %q", c.path, got, c.want)
		}
	}
}

func TestTheKeyHelpersRefuseBadQueries(t *testing.T) {
	t.Parallel()
	key := func(target string) (string, error) {
		r := httptest.NewRequest(http.MethodGet, target, nil)
		r.SetPathValue("slug", strings.TrimPrefix(strings.SplitN(r.URL.Path, "?", 2)[0], "/v1/x/"))
		q, err := Query(r, "range", "kind")
		if err != nil {
			return "", err
		}
		slug, err := PathSlug(r)
		if err != nil {
			return "", err
		}
		rangeID, err := QueryRange(q)
		if err != nil {
			return "", err
		}
		kind, err := QueryKind(q, "residential")
		return slug + "/" + kind + "/" + rangeID, err
	}
	for target, want := range map[string]string{"/v1/x/net00": "net00/residential/30d", "/v1/x/net00?range=24h&kind=mobile": "net00/mobile/24h"} {
		if got, err := key(target); got != want || err != nil {
			t.Errorf("%s: %q %v, want %q", target, got, err, want)
		}
	}
	for target, want := range map[string]string{"/v1/x/net00?range=1y": "range must be", "/v1/x/net00?page=2": "unknown query parameter page",
		"/v1/x/net00?kind=Mobile!": "invalid kind", "/v1/x/Net_00": "invalid network"} {
		var bad BadRequest
		if _, err := key(target); !errors.As(err, &bad) || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: %v, want %q", target, err, want)
		}
	}
}
