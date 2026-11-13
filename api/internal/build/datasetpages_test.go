package build_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/server"
)

// The dataset file alone regenerates every page answer the site draws: its
// `pages` holds, under each request path, exactly the bytes the server
// renders for that answer, and nothing else (no list page), with
// /v1/countries and each /v1/countries/{CC} (contract K11) and the
// registered answers (DATASET-PAGES-2). The expected paths are built here
// from the build's answers, not by the code under test.
func TestTheDatasetFileRegeneratesEveryPageAnswer(t *testing.T) {
	t.Parallel()
	file, res := v4File(t)
	var doc struct {
		Pages map[string]json.RawMessage `json:"pages"`
	}
	if err := json.Unmarshal(file, &doc); err != nil {
		t.Fatal(err)
	}
	pg := res.Pages
	want := map[string]any{"/v1/networks": pg.Networks}
	for rangeID, ans := range pg.Rankings {
		want["/v1/rankings?range="+rangeID] = ans
	}
	for key, ans := range pg.RankingsByKind {
		rangeID, kind, _ := strings.Cut(key, "/")
		want["/v1/rankings?range="+rangeID+"&kind="+kind] = ans
	}
	for _, ans := range pg.Network {
		want["/v1/networks/"+ans.Network.Slug+"?kind="+ans.Kind] = ans
	}
	for _, c := range pg.Charts {
		want["/v1/networks/"+c.Network+"/chart?kind="+c.Kind+"&range="+c.Range] = c
	}
	want["/v1/countries"] = pg.Countries
	for _, ans := range pg.Country {
		want["/v1/countries/"+ans.CountryCode] = ans
	}
	newPages := wantRegistered(res, want)
	if len(pg.Country) < 2 {
		t.Fatalf("fixture too small: %d country answers", len(pg.Country))
	}
	if len(pg.Rankings) != 3 || len(pg.Network) < 3 || len(pg.Charts) < 9 {
		t.Fatalf("fixture too small: %d rankings, %d network answers, %d charts", len(pg.Rankings), len(pg.Network), len(pg.Charts))
	}
	// 4 kinds x 3 ranges x 3 engine views of targets, every range of home
	// and events stats and the changelog, meta, overlap and a price history
	// per network.
	if newPages["targets"] != 36 || newPages["home"] != 3 || newPages["events-stats"] != 3 || newPages["meta"] != 1 || newPages["changelog"] != 3 ||
		newPages["overlap"] < 3 || newPages["pricing-history"] < 5*3 {
		t.Fatalf("fixture too small: %v", newPages)
	}
	for path, ans := range want {
		body, err := server.Marshal(ans)
		if err != nil {
			t.Fatal(err)
		}
		if got, ok := doc.Pages[path]; !ok || !bytes.Equal(got, body) {
			t.Errorf("%s: the file does not regenerate the served answer (in the file: %v)", path, ok)
		}
	}
	var extra []string
	for path := range doc.Pages {
		if _, ok := want[path]; !ok {
			extra = append(extra, path)
		}
	}
	sort.Strings(extra)
	if len(extra) > 0 {
		t.Errorf("pages the site does not draw: %s", strings.Join(extra, ", "))
	}
}

// wantRegistered adds to want the registered answers the file holds, under
// the request each body names, and counts them by answer: the per-network
// events stats and the paged reviews and runs stay out.
func wantRegistered(res *build.Result, want map[string]any) map[string]int {
	n := map[string]int{}
	bodies := []any{}
	for path, b := range res.Pages.Registered {
		if rec, ok := b.(*build.ChangeRecord); ok {
			// The record names no range: its stored path does.
			n["changelog"]++
			want["/v1/changelog?range="+strings.TrimPrefix(path, "/v1/changelog/")] = rec
			continue
		}
		bodies = append(bodies, b)
	}
	for _, b := range res.Pages.Held["pricing-history"] {
		bodies = append(bodies, b)
	}
	for _, b := range res.Events.Held["events-stats"] {
		bodies = append(bodies, b)
	}
	add := func(name, path string, body any) {
		n[name]++
		want[path] = body
	}
	for _, body := range bodies {
		switch a := body.(type) {
		case *build.TargetsAnswer:
			path := "/v1/targets?kind=" + a.Kind + "&range=" + a.Range
			if a.Engine != nil {
				path += "&engine=" + *a.Engine
			}
			add("targets", path, a)
		case *build.OverlapAnswer:
			add("overlap", "/v1/overlap?kind="+a.Kind+"&range="+a.Range, a)
		case *build.HomeAnswer:
			add("home", "/v1/home?range="+a.Range, a)
		case build.MetaAnswer:
			add("meta", "/v1/meta", a)
		case *build.PricingHistoryAnswer:
			path := "/v1/networks/" + a.Network + "/pricing-history"
			if !a.KindDefault {
				path += "?kind=" + a.Kind
			}
			add("pricing-history", path, a)
		case *build.EventsStatsAnswer:
			if a.Network == nil {
				add("events-stats", "/v1/events/stats?range="+a.Range, a)
			}
		}
	}
	return n
}

// Each answer's dataset key is a request its route serves with the body
// stored under it: Key(Request(path)) is path again.
func TestADatasetKeyIsTheRequestThatServesTheBody(t *testing.T) {
	t.Parallel()
	_, res := v4File(t)
	for _, a := range build.Answers() {
		if !a.InDataset {
			continue
		}
		var got string
		mux := http.NewServeMux()
		mux.HandleFunc(a.Route, func(_ http.ResponseWriter, r *http.Request) { got, _ = a.Key(r) })
		paths := res.Pages.Held[a.Name]
		if a.Events {
			paths = res.Events.Held[a.Name]
		} else if a.Cut == nil {
			paths = res.Pages.Registered
		}
		for path := range paths {
			if !a.Owns(path) {
				continue
			}
			req := a.Request(path)
			if req == "" {
				continue
			}
			got = ""
			mux.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, req, nil))
			if got != path {
				t.Errorf("%s: %s is served from %q, not %s", a.Name, req, got, path)
			}
		}
	}
}
