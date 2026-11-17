package server

import (
	"context"
	"encoding/json"
	"maps"
	"strings"
	"testing"

	"github.com/shifter-io/atlas/api/internal/build"
)

// Every range of home, rankings, targets and overlap, and the networks list,
// answer with the serving build's generatedAt (#1267).
func TestEveryRangeServesOneBuild(t *testing.T) {
	t.Parallel()
	s := newTestServer(t, &fakeSource{rev: 1})
	want := s.Current().Result.Dataset.GeneratedAt
	if len(ConsistencyRequests) != 13 {
		t.Fatalf("requests %v", ConsistencyRequests)
	}
	for _, req := range ConsistencyRequests {
		r := get(s.Handler(), req)
		var b struct{ GeneratedAt string }
		if err := json.Unmarshal(r.Body.Bytes(), &b); r.Code != 200 || err != nil || b.GeneratedAt != want {
			t.Errorf("%s: %d generatedAt %q, want %q (%v)", req, r.Code, b.GeneratedAt, want, err)
		}
	}
}

// withHome is res with its 30d home answer replaced by ans (nil: left out).
func withHome(res *build.Result, ans any) *build.Result {
	out, pg := *res, *res.Pages
	pg.Registered = maps.Clone(pg.Registered)
	if ans == nil {
		delete(pg.Registered, "/v1/home/30d")
	} else {
		pg.Registered["/v1/home/30d"] = ans
	}
	out.Pages = &pg
	return &out
}

// A build whose 30d home is the previous build's, or missing, is refused, so
// the serving build keeps answering every range (#1267: home?range=30d served
// the 15:10 build beside the 16:10 rankings).
func TestAStaleOrMissingRangeIsRefused(t *testing.T) {
	t.Parallel()
	s := newTestServer(t, &fakeSource{rev: 1})
	cur := s.Current()
	home, ok := cur.Result.Pages.Registered["/v1/home/30d"].(*build.HomeAnswer)
	if !ok {
		t.Fatal("no 30d home answer")
	}
	stale := *home
	stale.GeneratedAt = "2026-09-26T13:56:08.606Z"
	for _, c := range []struct {
		name string
		ans  any
		want string
	}{
		{"stale", &stale, "/v1/home?range=30d is from 2026-09-26T13:56:08.606Z"},
		{"missing", nil, "/v1/home?range=30d was not built"},
	} {
		_, err := renderVersion(context.Background(), 9, withHome(cur.Result, c.ans), s.opts, cur)
		if err == nil || !refused(err) || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: %v, want a refusal naming %q", c.name, err, c.want)
		}
	}
	if _, err := renderVersion(context.Background(), 10, withHome(cur.Result, home), s.opts, cur); err != nil {
		t.Fatalf("the build's own home answer: %v", err)
	}
}

func TestStoredPathsOfTheConsistencyRequests(t *testing.T) {
	t.Parallel()
	for req, want := range map[string]string{"/v1/networks": "/v1/networks", "/v1/rankings?range=7d": "/v1/rankings/7d",
		"/v1/home?range=24h": "/v1/home/24h"} {
		if got, err := storedPath(req); err != nil || got != want {
			t.Errorf("%s: %q %v, want %q", req, got, err, want)
		}
	}
	for _, req := range []string{"/v1/nothing", "/v1/rankings?range=1y", "%zz"} {
		if _, err := storedPath(req); err == nil {
			t.Errorf("%s: no error", req)
		}
	}
	if _, err := generatedAt(&Rendered{Identity: []byte(`{"range":"30d"}`)}); err == nil {
		t.Error("a body without generatedAt passed")
	}
}
