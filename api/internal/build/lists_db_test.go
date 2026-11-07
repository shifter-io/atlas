package build_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"reflect"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/server"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// keep is how many rows of each list the slimmed payload keeps (nine lists:
// the breakdowns and the pool lists).
var keep = map[string]int{"country": 1000, "region": 2, "city": 2, "asn": 2, "country_asn": 2, "city_asn": 1,
	"geo": 3, "countries_breakdown": 1000, "asns_breakdown": 2}

// listLabels are each dimension's rows: place (country, name, region code,
// region, city) and network (asn, name, usage type) labels.
var listLabels = map[string][][]any{
	"c": {{"US", "United States"}, {"DE", "Germany"}, {"FR", "France"}},
	"r": {{"DE", "Germany", "BE", "Berlin"}, {"DE", "Germany", "HH", "Hamburg"}, {"US", "United States", "CA", "California"}, {"FR", "France", "IDF", "Île-de-France"}},
	"g": {{"DE", "Germany", "BE", "Berlin", "Berlin"}, {"DE", "Germany", "HH", "Hamburg", "Hamburg"}, {"US", "United States", "CA", "California", "Los Angeles"},
		{"FR", "France", "IDF", "Île-de-France", "Paris"}, {"FR", "France", "IDF", "Île-de-France", `Saint-Denis "Nord"`}},
	// Two ASNs tie on IPs, in the reverse of Python's str() order.
	"a": {{3320, "Deutsche Telekom AG", "isp"}, {6805, "Telefonica Germany", "mobile"}, {3209, "Vodafone GmbH", "isp"}, {7922, "Comcast", "isp"},
		{3215, "Orange S.A.", "isp"}, {64496, "Idle Net", "hosting"}},
	"x": {{"DE", "Germany", 3320, "Deutsche Telekom AG", "isp"}, {"DE", "Germany", 3209, "Vodafone GmbH", "isp"}, {"US", "United States", 7922, "Comcast", "isp"}, {"FR", "France", 3215, "Orange S.A.", "isp"}},
	"y": {{"DE", "Germany", "BE", "Berlin", "Berlin", 3320, "Deutsche Telekom AG", "isp"}, {"DE", "Germany", "HH", "Hamburg", "Hamburg", 3209, "Vodafone GmbH", "isp"},
		{"US", "United States", "CA", "California", "Los Angeles", 7922, "Comcast", "isp"}, {"FR", "France", "IDF", "Île-de-France", "Paris", 3215, "Orange S.A.", "isp"},
		{"DE", "Germany", "BE", "Berlin", "Berlin", 6805, "Telefonica Germany", "mobile"}},
}

var listNames = map[string]string{"c": "country", "r": "region", "g": "city", "a": "asn", "x": "country_asn", "y": "city_asn"}

// writeList stores one list's typed rows (K2) of the fixture's product and
// returns its id. Counters differ per seed; success rates are never whole, the
// last row of a dimension has no request and the second no p99.
func writeList(t *testing.T, c *pgx.Conn, seed int) int64 {
	t.Helper()
	ctx := context.Background()
	product := testdb.Product(t, c, "shifter", "residential")
	var id int64
	if err := c.QueryRow(ctx, `INSERT INTO calculated_lists(`+testdb.ListColumns+`) VALUES ($1,$2,1,`+testdb.ListFill+`) RETURNING list_id`,
		product, fmt.Sprint("seed", seed)).Scan(&id); err != nil {
		t.Fatal(err)
	}
	for code, labels := range listLabels {
		for i, l := range labels {
			var place, network any
			s := func(i int) any { return l[i] }
			switch code {
			case "c":
				place = idOf(t, c, "place_names", "place_id", "country_code,country,region_code,region,city", s(0), s(1), nil, nil, nil)
			case "r":
				place = idOf(t, c, "place_names", "place_id", "country_code,country,region_code,region,city", s(0), s(1), s(2), s(3), nil)
			case "g", "y":
				place = idOf(t, c, "place_names", "place_id", "country_code,country,region_code,region,city", s(0), s(1), s(2), s(3), s(4))
			}
			switch code {
			case "a":
				network = asnLabel(t, c, l[0], l[1], l[2])
			case "x":
				place = idOf(t, c, "place_names", "place_id", "country_code,country,region_code,region,city", s(0), s(1), nil, nil, nil)
				network = asnLabel(t, c, l[2], l[3], l[4])
			case "y":
				network = asnLabel(t, c, l[5], l[6], l[7])
			}
			ips := int64(40 - 10*i + seed)
			if code == "a" && i == 2 {
				ips += 10 // ties with the row before
			}
			if code == "a" && i == len(labels)-1 {
				ips = 0
			}
			completed, successes := int64(100-10*i+seed), int64(97-10*i+seed)
			var p99 any = 300 + i
			if i == 1 {
				p99 = nil
			}
			if i == len(labels)-1 {
				completed, successes = 0, 0
			}
			if _, err := c.Exec(ctx, `INSERT INTO calculated_list_rows VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)`,
				id, code, i, place, network, ips, ips*3, completed, successes, i, 0, 100+i, 200+i, p99); err != nil {
				t.Fatal(err)
			}
		}
	}
	return id
}

func idOf(t *testing.T, c *pgx.Conn, table, key, columns string, values ...any) int64 {
	t.Helper()
	var id int64
	err := c.QueryRow(context.Background(), fmt.Sprintf(`WITH s AS (SELECT %[1]s FROM %[2]s WHERE (%[3]s) IS NOT DISTINCT FROM ($1,$2,$3,$4,$5)),
 i AS (INSERT INTO %[2]s(%[3]s) SELECT $1,$2,$3,$4,$5 WHERE NOT EXISTS (SELECT 1 FROM s) RETURNING %[1]s)
 SELECT %[1]s FROM s UNION ALL SELECT %[1]s FROM i`, key, table, columns), values...).Scan(&id)
	if err != nil {
		t.Fatal(err)
	}
	return id
}

func asnLabel(t *testing.T, c *pgx.Conn, asn, name, usage any) int64 {
	t.Helper()
	ctx := context.Background()
	var nameID, id int64
	if err := c.QueryRow(ctx, `INSERT INTO asn_names(asn,name) VALUES ($1,$2) ON CONFLICT (asn,name) DO UPDATE SET name=excluded.name RETURNING asn_name_id`, asn, name).Scan(&nameID); err != nil {
		t.Fatal(err)
	}
	if err := c.QueryRow(ctx, `INSERT INTO asn_labels(asn_name_id,usage_type) VALUES ($1,$2) ON CONFLICT (asn_name_id,usage_type) DO UPDATE SET usage_type=excluded.usage_type RETURNING asn_label_id`, nameID, usage).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return id
}

// oracle is a list as the published v2 payload has it, written by SQL (as
// jsonb writes the payload), independently of the API's Go writer: the first
// n breakdown rows by position, or the first n rows of a pool list (IPs above
// zero, most first; the fixture's ties sort as Python's str() does).
const oracle = `SELECT coalesce(jsonb_agg(x ORDER BY o), '[]')::text FROM (SELECT
 CASE WHEN $4 THEN jsonb_strip_nulls(jsonb_build_object('country_code',p.country_code,'country',p.country,'region',p.region,
  'region_code',p.region_code,'city',p.city,'asn',a.asn,'name',a.name,'usage_type',a.usage_type))
  || jsonb_build_object('ips',r.ips,'observations',r.observations)
 ELSE (CASE r.dimension WHEN 'c' THEN jsonb_build_object('country_code',p.country_code,'country',p.country)
  WHEN 'r' THEN jsonb_build_object('country_code',p.country_code,'country',p.country,'region',p.region,'region_code',p.region_code)
  WHEN 'g' THEN jsonb_build_object('country_code',p.country_code,'country',p.country,'region',p.region,'region_code',p.region_code,'city',p.city)
  WHEN 'x' THEN jsonb_build_object('country_code',p.country_code,'country',p.country,'asn',a.asn,'name',a.name,'usage_type',a.usage_type)
  WHEN 'y' THEN jsonb_build_object('country_code',p.country_code,'country',p.country,'region',p.region,'region_code',p.region_code,
   'city',p.city,'asn',a.asn,'name',a.name,'usage_type',a.usage_type)
  ELSE jsonb_build_object('asn',a.asn,'name',a.name,'usage_type',a.usage_type) END)
  || jsonb_build_object('ips',r.ips,'observations',r.observations,'completed',r.completed,'successes',r.successes,
   'success_rate',r.successes::float8/nullif(r.completed,0),'challenges',r.challenges,'blocks',r.blocks,'p50',r.p50,'p90',r.p90,'p99',r.p99) END x,
 row_number() OVER (ORDER BY CASE WHEN $4 THEN -r.ips END, CASE WHEN $4 THEN coalesce(a.asn::text, p.country_code) END, r.position) o
 FROM calculated_list_rows r LEFT JOIN place_names p USING(place_id) LEFT JOIN asn_label_names a USING(asn_label_id)
 WHERE r.list_id = $1 AND r.dimension = $2 AND (NOT $4 OR r.ips > 0) ORDER BY o LIMIT $3) s`

func oracleList(t *testing.T, c *pgx.Conn, list int64, code string, n int, pool bool) (json.RawMessage, int) {
	t.Helper()
	var text string
	if err := c.QueryRow(context.Background(), oracle, list, code, n, pool).Scan(&text); err != nil {
		t.Fatal(err)
	}
	var whole string
	if err := c.QueryRow(context.Background(), oracle, list, code, 100000, pool).Scan(&whole); err != nil {
		t.Fatal(err)
	}
	var rows []any
	json.Unmarshal([]byte(whole), &rows) //nolint:errcheck
	return json.RawMessage(text), len(rows)
}

// listedPayloads is the every-request fixture with every window's lists made
// from the typed list lists[label]: v2 slimmed to keep, and v3 naming them.
func listedPayloads(t *testing.T, c *pgx.Conn, lists map[string]int64) (v2, v3 []byte) {
	t.Helper()
	raw, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil {
		t.Fatal(err)
	}
	for _, version := range []int{2, 3} {
		var doc map[string]any
		if err := json.Unmarshal(raw, &doc); err != nil {
			t.Fatal(err)
		}
		for label, entry := range doc["products"].([]any)[0].(map[string]any)["windows"].(map[string]any) {
			w, id := entry.(map[string]any), lists[label]
			pool := w["pool"].(map[string]any)
			breakdowns, entries, kept := map[string]any{}, map[string]any{}, map[string]any{}
			for code, name := range listNames {
				rows, n := oracleList(t, c, id, code, keep[name], false)
				breakdowns[name], entries[name], kept[name] = rows, n, min(n, keep[name])
			}
			poolEntries := map[string]any{}
			for name, code := range map[string]string{"geo": "g", "countries_breakdown": "c", "asns_breakdown": "a"} {
				rows, n := oracleList(t, c, id, code, keep[name], true)
				pool[name], poolEntries[name], kept[name] = rows, n, min(n, keep[name])
			}
			w["breakdowns"], w["breakdown_entries"], pool["list_entries"] = breakdowns, entries, poolEntries
			if version == 3 {
				delete(w, "breakdowns")
				for _, name := range []string{"geo", "countries_breakdown", "asns_breakdown"} {
					delete(pool, name)
				}
				w["lists"] = map[string]any{"id": id, "entries": kept}
			}
		}
		doc["schema_version"] = version
		b, err := json.Marshal(doc)
		if err != nil {
			t.Fatal(err)
		}
		if version == 2 {
			v2 = b
		} else {
			v3 = b
		}
	}
	return v2, v3
}

// linkWindows writes a v2 build's target-free windows with their list ids.
func linkWindows(t *testing.T, db *testdb.DB, build int64, lists map[string]int64) {
	t.Helper()
	for label, id := range lists {
		hours := map[string]int{"24h": 24, "7d": 168, "30d": 720}[label]
		db.Exec(t, `INSERT INTO cohorts(cohort) VALUES ('7a5d84127c244707dc0e') ON CONFLICT DO NOTHING`)
		db.Exec(t, `INSERT INTO calculated_windows(build_id,product_id,cohort,target,window_hours,completed,successes,errors,body_bytes,
 coverage,readiness,list_id) VALUES ($1,1,'7a5d84127c244707dc0e','',$2,0,0,0,0,0,'ready',$3)`, build, hours, id)
	}
}

func loadWith(t *testing.T, db *testdb.DB, lists *store.Lists) *build.Result {
	t.Helper()
	pool, err := store.OpenReadOnly(context.Background(), db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	return loadFrom(t, pool, lists)
}

// loadFrom loads and builds the current build through db.
func loadFrom(t *testing.T, db store.TxBeginner, lists *store.Lists) *build.Result {
	t.Helper()
	in, err := store.LoadLists(context.Background(), db, lists)
	if err != nil {
		t.Fatal(err)
	}
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	return res
}

// datasetText is the rendered dataset without the build's content hash.
func datasetText(t *testing.T, res *build.Result) []byte {
	t.Helper()
	ds := *res.Dataset
	ds.Snapshot.ContentHash, ds.Snapshot.SchemaVersion = nil, 0 // v3 says 3
	b, err := server.Marshal(&ds)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestV3PayloadAnswersAsItsV2Form(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	c := db.Conn(t)
	defer c.Close(context.Background())
	lists := map[string]int64{"24h": writeList(t, c, 0), "7d": writeList(t, c, 1), "30d": writeList(t, c, 2)}
	v2, v3 := listedPayloads(t, c, lists)
	var doc struct {
		Products []struct {
			Windows map[string]struct {
				Breakdowns any `json:"breakdowns"`
				Pool       struct {
					Geo any `json:"geo"`
				} `json:"pool"`
			} `json:"windows"`
		} `json:"products"`
	}
	if err := json.Unmarshal(v3, &doc); err != nil || doc.Products[0].Windows["24h"].Breakdowns != nil || doc.Products[0].Windows["24h"].Pool.Geo != nil {
		t.Fatalf("the v3 payload carries lists (%v)", err)
	}
	cache := store.NewLists()
	linkWindows(t, db, db.Publish(t, v2), lists)
	fromV2 := loadWith(t, db, cache)
	if cache.Fetched != 3 {
		t.Errorf("v2 build fetched %d lists, want 3", cache.Fetched)
	}
	db.Publish(t, v3)
	fromV3 := loadWith(t, db, cache)
	if cache.Fetched != 3 {
		t.Errorf("the v3 build of the same lists fetched %d more, want none", cache.Fetched-3)
	}
	fresh := loadWith(t, db, nil)
	for name, res := range map[string]*build.Result{"cached": fromV3, "fresh": fresh} {
		if a, b := datasetText(t, fromV2), datasetText(t, res); !bytes.Equal(a, b) {
			var diffs []string
			diff(generic(t, a), generic(t, b), "dataset", &diffs)
			t.Errorf("%s v3 dataset differs from v2 (%d bytes, %d bytes):\n%v", name, len(a), len(b), diffs)
		}
		if !reflect.DeepEqual(generic(t, fromV2.Details["shifter"]), generic(t, res.Details["shifter"])) {
			t.Errorf("%s v3 provider differs from v2", name)
		}
		for _, r := range []string{"24h", "7d", "30d"} {
			for _, d := range []string{"country", "region", "city", "asn"} {
				w, _, _ := fromV2.Pages.List(context.Background(), "", "shifter/residential", r, d, "", 1)
				g, _, _ := res.Pages.List(context.Background(), "", "shifter/residential", r, d, "", 1)
				if !reflect.DeepEqual(generic(t, w), generic(t, g)) {
					t.Errorf("%s %s %s: v3 page %+v, v2 page %+v", name, r, d, g, w)
				}
			}
		}
	}
}

// networkPayload is the every-request fixture as payload v4 (its group's
// network, testscale.NetworkHead) whose windows name the typed lists
// lists[label], and the group.
func networkPayload(t *testing.T, c *pgx.Conn, lists map[string]int64) (head []byte, groups [][]byte) {
	t.Helper()
	raw, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Products []json.RawMessage `json:"products"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	head, err = testscale.NetworkHead(&store.SnapshotRow{Payload: raw}, 0, func(w testscale.NetWindow, _ map[string][]store.NetRow) (int64, map[string]int, error) {
		counts := map[string]int{}
		for code, name := range netCodes {
			_, counts[name] = oracleList(t, c, lists[w.Label], code, 0, false)
		}
		return lists[w.Label], counts, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return head, [][]byte{doc.Products[0]}
}

// loggedPool is a pool to db whose connections log their statements.
func loggedPool(t *testing.T, db *testdb.DB) (*pgxpool.Pool, *statementLog) {
	t.Helper()
	cfg, err := pgxpool.ParseConfig(db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	log := &statementLog{}
	cfg.ConnConfig.Tracer = log
	pool, err := pgxpool.NewWithConfig(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	return pool, log
}

// A v4 build reads only the network lists the API does not hold yet, and
// the API drops those the build no longer names.
func TestOnlyNewListsAreFetched(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	c := db.Conn(t)
	defer c.Close(context.Background())
	first := map[string]int64{"24h": writeList(t, c, 0), "7d": writeList(t, c, 1), "30d": writeList(t, c, 2)}
	pool, log := loggedPool(t, db)
	cache := store.NewLists()
	head, groups := networkPayload(t, c, first)
	db.PublishParts(t, head, groups, nil)
	loadFrom(t, pool, cache)
	// The next build shares the 7d and 30d lists and has a new 24h one.
	second := map[string]int64{"24h": writeList(t, c, 3), "7d": first["7d"], "30d": first["30d"]}
	head, groups = networkPayload(t, c, second)
	db.PublishParts(t, head, groups, nil)
	res := loadFrom(t, pool, cache)
	if len(log.counts) != 4 {
		t.Errorf("fetched %d lists over two builds, want 3 + 1", len(log.counts))
	}
	if want := datasetText(t, loadWith(t, db, nil)); !bytes.Equal(datasetText(t, res), want) {
		t.Error("the cached build differs from a fresh read")
	}
	// The first 24h list is no longer referenced: it was dropped.
	head, groups = networkPayload(t, c, first)
	db.PublishParts(t, head, groups, nil)
	loadFrom(t, pool, cache)
	if len(log.counts) != 5 {
		t.Errorf("fetched %d lists after going back, want 5 (the dropped list read again)", len(log.counts))
	}
}
