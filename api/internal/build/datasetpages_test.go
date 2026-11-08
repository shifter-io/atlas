package build_test

import (
	"bytes"
	"encoding/json"
	"sort"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/server"
)

// The dataset file alone regenerates every page answer the site draws: its
// `pages` holds, under each request path, exactly the bytes the server
// renders for that answer, and nothing else (no list page), with
// /v1/countries and each /v1/countries/{CC} (contract K11). The expected
// paths are built here from the build's answers, not by the code under test.
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
	if len(pg.Country) < 2 {
		t.Fatalf("fixture too small: %d country answers", len(pg.Country))
	}
	if len(pg.Rankings) != 3 || len(pg.Network) < 3 || len(pg.Charts) < 9 {
		t.Fatalf("fixture too small: %d rankings, %d network answers, %d charts", len(pg.Rankings), len(pg.Network), len(pg.Charts))
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
