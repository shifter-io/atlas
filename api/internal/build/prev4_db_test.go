package build_test

// The proofs, while the store still reads builds before payload v4, that the
// tests moved onto v4 builds (API-TESTS-V4-3) expect the answers they
// expected before, but for the documented differences of a v4 build
// (testscale.AsV4Answers), and the goldens' source. API-SUM-CLEAN-1a/1b
// (#634/#635) delete this file with the pre-v4 path.

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"os"
	"slices"
	"sort"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// TestTheMovedBuildsAnswerAsBeforeV4: every payload a moved test publishes
// with testdb.PublishV4, over the test's database rows, gives every answer
// the build before v4 of the payload's lists from one source
// (testscale.Listed) gives, but for the documented differences
// (testscale.AsV4Answers, and the dataset file's lists and coverage evidence:
// withoutV4Dataset). The saved fixtures' pool lists and breakdowns disagree
// (Listed), which alone moves an expected figure from the build of the
// payload itself.
func TestTheMovedBuildsAnswerAsBeforeV4(t *testing.T) {
	t.Parallel()
	read := func(name string) []byte {
		b, err := os.ReadFile(repoFile(t, "fixtures", "measured", name))
		if err != nil {
			t.Fatal(err)
		}
		return b
	}
	every := read("snapshot-every-request.json")
	browser, err := testscale.WithBrowser(every)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		name, rows string
		payload    []byte
	}{
		{"partial", testCatalog + acmeCatalog, []byte(partialSnapshot)},
		{"outages", testCatalog, withOutages(t)},
		{"every request", testCatalog, every},
		{"city x asn", "", withCityAsn(t)},
		{"v1", testCatalog, read("snapshot.json")},
		{"early trend", testCatalog, withEarlyTrend(t)},
		{"browser", testscale.BrowserTargetsSQL(), browser},
		{"target groups", testscale.BrowserTargetsSQL(), withTargetGroups(t)},
		{"estimates", hourReleased, estimatedSnapshot(t)},
		{"latency mean", testCatalog, everyRequestWithMean(t, 1210.37)},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			listed, err := testscale.Listed(c.payload)
			if err != nil {
				t.Fatal(err)
			}
			db := testdb.New(t)
			db.Publish(t, listed)
			if c.rows != "" {
				db.Exec(t, c.rows)
			}
			want, err := testscale.AsV4Answers(currentAnswers(t, db), payloadGroups(t, c.payload))
			if err != nil {
				t.Fatal(err)
			}
			db.PublishV4(t, c.payload)
			compareAnswers(t, "v4", want, currentAnswers(t, db))
		})
	}
}

// currentAnswers is every answer of the current build (netAnswers), its
// later pages read from the database.
func currentAnswers(t *testing.T, db *testdb.DB) map[string][]byte {
	t.Helper()
	db.Exec(t, "UPDATE calculated_snapshots SET content_hash = 'same'")
	pool, err := store.OpenReadOnly(context.Background(), db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	return netAnswers(t, loadFrom(t, pool, nil))
}

// payloadGroups is a payload's product groups.
func payloadGroups(t *testing.T, payload []byte) [][]byte {
	t.Helper()
	var s struct {
		Products []json.RawMessage `json:"products"`
	}
	if err := json.Unmarshal(payload, &s); err != nil {
		t.Fatal(err)
	}
	out := make([][]byte, len(s.Products))
	for i, g := range s.Products {
		out[i] = g
	}
	return out
}

// TestTheNetworkGoldensAreTheV3Answers is the comparison
// TestANetworkOfOneGroupAnswersAsTheGroup made before its golden: the v3
// build of its data reads its parts and its 9 group lists, and its answers
// but for the documented id, cohort and schema version strings, and its
// coverage evidence, are the v4 build's and the golden's; a v3 build read
// after the v4 one reads both again and answers as before.
func TestTheNetworkGoldensAreTheV3Answers(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	c := db.Conn(t)
	defer c.Close(context.Background())
	nc := writeNetLists(t, c)
	v3, v4, groups := netPayloads(t, c, nc)
	same := func() { db.Exec(t, "UPDATE calculated_snapshots SET content_hash = 'same'") }
	cache := store.NewLists()
	db.PublishParts(t, v3, groups, nil)
	same()
	fromV3, log3 := loadLogged(t, db, cache)
	if !log3.reads("calculated_snapshot_parts") || !subset(log3.listIDs, nc.groupLists) || cache.Fetched != 9 {
		t.Errorf("the v3 build: parts read %v, list ids %v, %d lists fetched; want its parts and its 9 group lists", log3.reads("calculated_snapshot_parts"), log3.listIDs, cache.Fetched)
	}
	db.PublishParts(t, v4, groups, nil)
	same()
	fromV4, _ := loadLogged(t, db, cache)
	want, err := testscale.AsV4Answers(netAnswers(t, fromV3), groups)
	if err != nil {
		t.Fatal(err)
	}
	evidence := evidenceRows(t, fromV3)
	if !slices.ContainsFunc(slices.Collect(maps.Values(evidence)), func(b []byte) bool { return bytes.Contains(b, []byte(`"rows":[[`)) }) {
		t.Error("no City x ASN rows compared")
	}
	maps.Copy(want, evidence)
	compareAnswers(t, "v4", want, oneGroupAnswers(t, fromV4))
	isGolden(t, oneGroupGolden, want)
	db.PublishParts(t, v3, groups, nil)
	same()
	again, log := loadLogged(t, db, cache)
	if !log.reads("calculated_snapshot_parts") || cache.Fetched != 18 {
		t.Errorf("the v3 build again: parts read %v, %d group lists fetched, want 18", log.reads("calculated_snapshot_parts"), cache.Fetched)
	}
	compareAnswers(t, "v3 again", netAnswers(t, fromV3), netAnswers(t, again))
}

// TestTheLongListGoldenIsTheV3Answers is the comparison
// TestLongNetworkListsPageFromTheDatabase made before its golden: the v3
// build of its rows answers, but for the documented strings, as the v4 build
// read from the database and the golden do.
func TestTheLongListGoldenIsTheV3Answers(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	c := db.Conn(t)
	defer c.Close(context.Background())
	nc := writeNetLists(t, c)
	tailLists(t, c, nc)
	v3, v4, groups := netPayloads(t, c, nc)
	db.PublishParts(t, v3, groups, nil)
	want, err := testscale.AsV4Answers(currentAnswers(t, db), groups)
	if err != nil {
		t.Fatal(err)
	}
	db.PublishParts(t, v4, groups, nil)
	compareAnswers(t, "v4 from the database", want, currentAnswers(t, db))
	isGolden(t, longListsGolden, want)
}

// evidenceRows is a build's coverage evidence (before v4) as cityAsnLists
// has a v4 build's: per network, kind and range, the rows with IPs, most
// first (ties in list order), their count and the first page of them.
func evidenceRows(t *testing.T, res *build.Result) map[string][]byte {
	t.Helper()
	out := map[string][]byte{}
	for _, p := range res.Dataset.Providers {
		for kind, byRange := range p.CoverageEvidence {
			for rangeID, rows := range byRange {
				var with []model.CoverageRow
				for _, r := range rows {
					if r.Ips.OK() && r.Ips > 0 {
						with = append(with, r)
					}
				}
				sort.SliceStable(with, func(i, j int) bool { return with[i].Ips > with[j].Ips })
				var page []string
				for _, r := range with[:min(len(with), build.PageSize)] {
					page = append(page, string(must(json.Marshal(r))))
				}
				out[fmt.Sprintf("city_asn %s %s %s", p.Slug, kind, rangeID)] = coverageAnswer(len(with), page)
			}
		}
	}
	return out
}

// isGolden checks that the golden name holds want, unless the goldens are
// being written (SITEAPI_UPDATE_GOLDENS, golden).
func isGolden(t *testing.T, name string, want map[string][]byte) {
	t.Helper()
	if os.Getenv("SITEAPI_UPDATE_GOLDENS") == "" {
		compareAnswers(t, "golden "+name, want, readGolden(t, name))
	}
}
