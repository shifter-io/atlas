package build_test

import (
	"bytes"
	"context"
	"fmt"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/server"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/jackc/pgx/v5"
)

// tailRows is how many rows tailLists appends to a list's region, city and
// City x ASN rows: more than a page.
const tailRows = 55

// tailLists appends to every list of nc a long tail of German regions, cities
// and City x ASN rows with few IPs, the last city named Zyx (no other row
// holds that word), and recounts nc's network list rows.
func tailLists(t *testing.T, c *pgx.Conn, nc netCase) {
	t.Helper()
	ctx := context.Background()
	if _, err := c.Exec(ctx, fmt.Sprintf(`INSERT INTO place_names(country_code,country,region_code,region,city)
 SELECT 'DE','Germany','T'||k,'Tail Region '||k,NULL FROM generate_series(1,%[1]d) k
 UNION ALL SELECT 'DE','Germany','T'||k,'Tail Region '||k,CASE WHEN k = %[1]d THEN 'Zyx' ELSE 'Tail City '||k END FROM generate_series(1,%[1]d) k`, tailRows)); err != nil {
		t.Fatal(err)
	}
	asn := asnLabel(t, c, 65001, "Tail Net", "isp")
	for _, lists := range []map[string]map[string]int64{nc.groupLists, nc.netLists} {
		for _, byLabel := range lists {
			for _, id := range byLabel {
				if _, err := c.Exec(ctx, `INSERT INTO calculated_list_rows(list_id,dimension,position,place_id,asn_label_id,ips,observations,
 completed,successes,challenges,blocks,p50,p90,p99)
 SELECT $1, v.d::"char", v.base + k - 1, p.place_id, CASE WHEN v.d = 'y' THEN $2::int END, 1 + k % 3, 3, 10 + k % 4, 9, 0, 0, 100 + k, 200, 300
 FROM (VALUES ('r', $3::int), ('g', $4::int), ('y', $5::int)) v(d, base) CROSS JOIN generate_series(1, $6::int) k
 JOIN place_names p ON p.country_code = 'DE' AND p.region_code = 'T'||k AND (p.city IS NULL) = (v.d = 'r')`,
					id, asn, len(listLabels["r"]), len(listLabels["g"]), len(listLabels["y"]), tailRows); err != nil {
					t.Fatal(err)
				}
			}
		}
	}
	for label, id := range nc.netLists[netProviders[0]] {
		for code, name := range netCodes {
			_, nc.counts[label][name] = oracleList(t, c, id, code, 0, false)
		}
	}
}

// longListsGolden is the golden of TestLongNetworkListsPageFromTheDatabase:
// the answers of the v3 build of its rows (its lists held in memory), but for
// the documented id, cohort and schema version strings
// (TestTheLongListGoldenIsTheV3Answers).
const longListsGolden = "network-long-lists"

// TestLongNetworkListsPageFromTheDatabase: a v4 build's lists longer than a
// page are read from the database when a page or search asks for them, and
// every answer equals, byte for byte, the golden's (the v3 build's of the
// same rows, its lists held in memory) and the v4 build's whose network
// lists are held in memory: later pages, a search only a tail row matches, a
// country's merged cities and regions, the all-networks list, the Coverage
// pages, the place pages and the dataset file. An unknown list is still not
// found.
func TestLongNetworkListsPageFromTheDatabase(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	c := db.Conn(t)
	defer c.Close(context.Background())
	nc := writeNetLists(t, c)
	tailLists(t, c, nc)
	v4, groups := netPayloads(t, c, nc)
	db.PublishParts(t, v4, groups, nil)
	db.Exec(t, "UPDATE calculated_snapshots SET content_hash = 'same'")
	ctx := context.Background()
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	in, err := store.LoadLists(ctx, pool, store.NewLists())
	if err != nil {
		t.Fatal(err)
	}
	fromDB, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	if n := fromDB.Pages.Network["shifter/residential"].Ranges["30d"].Lists["city"]; n != len(listLabels["g"])+tailRows {
		t.Fatalf("shifter's 30d city list has %d rows, want %d", n, len(listLabels["g"])+tailRows)
	}
	golden(t, longListsGolden, netAnswers(t, fromDB))
	held := heldBuild(t, pool, in, nc)
	compareAnswers(t, "v4 held in memory", longPages(t, held), longPages(t, fromDB))
	if page, ok, err := fromDB.Pages.List(ctx, "", "country/DE", "30d", "city", build.ListFilter{Query: "zyx"}, 1); err != nil || !ok || len(page.Rows) != len(netProviders) ||
		page.Rows[0].City == nil || *page.Rows[0].City != "Zyx" {
		t.Errorf("a search for the last tail city: %v %v %+v", err, ok, page)
	}
	if _, ok, err := fromDB.Pages.List(ctx, "", "nobody/residential", "30d", "city", build.ListFilter{}, 2); ok || err != nil {
		t.Errorf("an unknown network's list: found %v, %v", ok, err)
	}
	if _, ok, err := fromDB.Pages.Coverage(ctx, "", "nobody/residential", "30d", build.ListFilter{}, 2); ok || err != nil {
		t.Errorf("an unknown network's Coverage: found %v, %v", ok, err)
	}
}

// heldBuild is the build of in's database read again, its network lists
// replaced by copies held in memory (store.NewNetList) of the same rows.
func heldBuild(t *testing.T, pool store.TxBeginner, in *store.Inputs, nc netCase) *build.Result {
	t.Helper()
	again, err := store.LoadLists(context.Background(), pool, nil)
	if err != nil {
		t.Fatal(err)
	}
	hours := map[string]int64{"24h": 24, "7d": 168, "30d": 720}
	lists := map[int64]*store.NetList{}
	for i, p := range netProviders {
		for label, id := range nc.netLists[p] {
			l := in.Snapshot.Networks[store.PlaceWindow{Product: int64(i + 1), Cohort: store.NetworkCohort, Hours: hours[label]}]
			rows := map[string][]store.NetRow{}
			for _, name := range netCodes {
				v, err := l.Rows(context.Background(), name)
				if err != nil {
					t.Fatal(err)
				}
				for j := range v.Len() {
					rows[name] = append(rows[name], v.Row(j))
				}
			}
			if lists[id], err = store.NewNetList(rows); err != nil {
				t.Fatal(err)
			}
		}
	}
	s := &store.SnapshotRow{BuildID: again.Snapshot.BuildID, ContentHash: again.Snapshot.ContentHash, Payload: again.Snapshot.Payload}
	if err := s.SetNetworks(lists); err != nil {
		t.Fatal(err)
	}
	again.Snapshot, again.ReadAt = s, in.ReadAt
	res, err := build.Build(again)
	if err != nil {
		t.Fatal(err)
	}
	return res
}

// longPages is every page, and searches, of every place list and Coverage
// answer of the case's networks, all networks and Germany, the place pages
// of Germany's regions and cities, and the dataset file without the
// registered answers' pages.
func longPages(t *testing.T, res *build.Result) map[string][]byte {
	t.Helper()
	ctx := context.Background()
	out := map[string][]byte{}
	keep := func(name string, v any, ok bool, err error) {
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if out[name], err = server.Marshal(map[string]any{"ok": ok, "answer": v}); err != nil {
			t.Fatal(err)
		}
	}
	scopes := map[string][]string{"country/DE": {"city", "region"}, build.AllNetworks + "/" + build.AllNetworks: {"country", "region", "city", "asn"}}
	for _, p := range netProviders {
		scopes[p+"/residential"] = scopes[build.AllNetworks+"/"+build.AllNetworks]
	}
	keys := map[string]bool{}
	for scope, dims := range scopes {
		for _, r := range netLabels {
			for _, d := range dims {
				for _, q := range []string{"", "zyx", "tail", "tail region 5"} {
					for page := 1; page <= 5; page++ {
						a, ok, err := res.Pages.List(ctx, "", scope, r, d, build.ListFilter{Query: q}, page)
						keep(fmt.Sprintf("places %s %s %s %d %q", scope, r, d, page, q), a, ok, err)
						for i := 0; scope == "country/DE" && a != nil && i < len(a.Rows); i++ {
							keys[d+"\x00"+a.Rows[i].Key] = true
						}
					}
				}
			}
		}
	}
	for _, p := range netProviders {
		for _, r := range netLabels {
			for _, q := range []string{"", "zyx", "tail net"} {
				for page := 1; page <= 4; page++ {
					a, ok, err := res.Pages.Coverage(ctx, "", p+"/residential", r, build.ListFilter{Query: q}, page)
					keep(fmt.Sprintf("coverage %s %s %d %q", p, r, page, q), a, ok, err)
				}
			}
		}
	}
	for k := range keys {
		d, key, _ := strings.Cut(k, "\x00")
		a, ok, err := res.Pages.Place(ctx, "", d, key, "30d")
		keep(fmt.Sprintf("place %s %s", d, key), a, ok, err)
	}
	var file bytes.Buffer
	if err := build.WriteDataset(&file, withoutRegisteredPages(res.Dataset)); err != nil {
		t.Fatal(err)
	}
	out["dataset file"] = file.Bytes()
	return out
}
