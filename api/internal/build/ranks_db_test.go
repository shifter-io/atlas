package build_test

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"math/rand"
	"os"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
)

// rankPlace is one row of a seeded place list.
type rankPlace struct {
	cc, regionCode, region, city string
	asn, ips, successes, p50     int64
}

var rankPlacePool = map[string][]rankPlace{
	"country": {{cc: "US"}, {cc: "DE"}, {cc: "RO"}, {cc: "FR"}, {cc: "GB"}},
	"region": {{cc: "US", regionCode: "CA", region: "California"}, {cc: "US", regionCode: "NY", region: "New York"},
		{cc: "DE", regionCode: "BE", region: "Berlin"}, {cc: "RO", regionCode: "B", region: "Bucharest"}},
	"city": {{cc: "US", regionCode: "CA", region: "California", city: "Los Angeles"},
		{cc: "US", regionCode: "CA", region: "California", city: "San Jose"},
		{cc: "DE", regionCode: "BE", region: "Berlin", city: "Berlin"},
		{cc: "RO", regionCode: "B", region: "Bucharest", city: "Bucharest"}},
	"asn": {{asn: 3320}, {asn: 8708}, {asn: 7922}, {asn: 9050}, {asn: 1}},
}

var rankDimensionCode = map[string]string{"country": "c", "region": "r", "city": "g", "asn": "a"}

// seededPlaces is a random whole list of a dimension: distinct places, with
// zero IPs, zero successes and tied values among them.
func seededPlaces(rng *rand.Rand, dimension string) []rankPlace {
	var out []rankPlace
	for _, i := range rng.Perm(len(rankPlacePool[dimension])) {
		if rng.Intn(4) == 0 {
			continue
		}
		p := rankPlacePool[dimension][i]
		p.ips, p.successes, p.p50 = int64(rng.Intn(5)), int64(rng.Intn(3)), int64(50+rng.Intn(4))
		out = append(out, p)
	}
	return out
}

func (p rankPlace) payloadRow() map[string]any {
	row := map[string]any{"ips": p.ips, "observations": p.ips, "completed": p.successes, "successes": p.successes, "p50": p.p50}
	if p.cc == "" {
		row["asn"], row["name"], row["usage_type"] = p.asn, fmt.Sprint("AS", p.asn), "isp"
		return row
	}
	row["country_code"], row["country"] = p.cc, p.cc
	if p.region != "" {
		row["region_code"], row["region"] = p.regionCode, p.region
	}
	if p.city != "" {
		row["city"] = p.city
	}
	return row
}

// rankedSnapshot publishes the every-request fixture as four networks with
// seeded coverage counts and place lists, the region, city and ASN lists capped
// to 2 rows the way slim() caps them: as payload v4 (each network its group,
// testdb.PublishV4) when v4, else as it is, with the typed rows of every whole
// list. Returns the build id.
func rankedSnapshot(t *testing.T, db *testdb.DB, v4 bool) int64 {
	raw, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	c := db.Conn(t)
	defer c.Close(ctx)
	one := func(sql string, args ...any) int64 {
		var v int64
		if err := c.QueryRow(ctx, sql, args...).Scan(&v); err != nil {
			t.Fatalf("%v\n%s", err, sql)
		}
		return v
	}
	template, _ := json.Marshal(doc["products"].([]any)[0])
	rng := rand.New(rand.NewSource(3))
	var products []any
	whole := map[[2]any]map[string][]rankPlace{} // product id, hours
	for _, provider := range []string{"alpha", "beta", "gamma", "delta"} {
		var p map[string]any
		if err := json.Unmarshal(template, &p); err != nil {
			t.Fatal(err)
		}
		db.Exec(t, "INSERT INTO providers VALUES ($1)", provider)
		id := one("INSERT INTO products(provider,name) VALUES ($1,'residential') RETURNING product_id", provider)
		p["provider"], p["name"], p["product_id"], p["id"] = provider, provider, id, fmt.Sprint(id, ":", p["cohort"])
		for _, entry := range p["windows"].(map[string]any) {
			w := entry.(map[string]any)
			pool := w["pool"].(map[string]any)
			for _, k := range []string{"unique_ips", "countries", "regions", "cities", "asns"} {
				pool[k] = rng.Intn(4)
			}
			lists, entries := map[string]any{}, map[string]any{}
			hours := int64(w["period"].(map[string]any)["hours"].(float64))
			whole[[2]any{id, hours}] = map[string][]rankPlace{}
			for dimension := range rankDimensionCode {
				all := seededPlaces(rng, dimension)
				whole[[2]any{id, hours}][dimension] = all
				kept := all
				if dimension != "country" && len(kept) > 2 {
					kept = kept[:2]
				}
				rows := []any{}
				for _, r := range kept {
					rows = append(rows, r.payloadRow())
				}
				lists[dimension], entries[dimension] = rows, len(all)
			}
			w["breakdowns"], w["breakdown_entries"] = lists, entries
		}
		products = append(products, p)
	}
	doc["products"] = products
	payload, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	if v4 {
		return db.PublishV4(t, payload)
	}
	build := db.Publish(t, payload)
	storeRankRows(t, db, build, products[0].(map[string]any)["cohort"].(string), whole)
	return build
}

// storeRankRows writes the typed rows calculate() persists for whole place
// lists, as calculated v8 stores them (testdb.CalculatedV8).
func storeRankRows(t *testing.T, db *testdb.DB, build int64, cohort string, whole map[[2]any]map[string][]rankPlace) {
	ctx := context.Background()
	c := db.Conn(t)
	defer c.Close(ctx)
	one := func(sql string, args ...any) int64 {
		var v int64
		if err := c.QueryRow(ctx, sql, args...).Scan(&v); err != nil {
			t.Fatalf("%v\n%s", err, sql)
		}
		return v
	}
	null := func(s string) any {
		if s == "" {
			return nil
		}
		return s
	}
	db.Exec(t, "INSERT INTO cohorts(cohort) VALUES ($1) ON CONFLICT DO NOTHING", cohort)
	db.CalculatedV8(t)
	db.Exec(t, fmt.Sprintf("CREATE TABLE calculated_breakdowns_b%d PARTITION OF calculated_breakdowns FOR VALUES IN (%d)", build, build))
	for k, lists := range whole {
		result := one(`INSERT INTO calculated_windows(build_id,product_id,cohort,target,window_hours,completed,successes,errors,
 body_bytes,coverage,readiness) VALUES ($1,$2,$3,'',$4,0,0,0,0,0,'ready') RETURNING result_id`, build, k[0], cohort, k[1])
		for dimension, rows := range lists {
			for i, r := range rows {
				var place, label any
				if dimension == "asn" {
					name := one(`INSERT INTO asn_names(asn,name) VALUES ($1,$2) ON CONFLICT (asn,name) DO UPDATE SET asn=excluded.asn
 RETURNING asn_name_id`, r.asn, fmt.Sprint("AS", r.asn))
					label = one(`INSERT INTO asn_labels(asn_name_id,usage_type) VALUES ($1,'isp')
 ON CONFLICT (asn_name_id,usage_type) DO UPDATE SET usage_type=excluded.usage_type RETURNING asn_label_id`, name)
				} else {
					place = one(`INSERT INTO place_names(country_code,country,region_code,region,city) VALUES ($1,$1,$2,$3,$4)
 ON CONFLICT (country_code,country,region_code,region,city) DO UPDATE SET city=excluded.city RETURNING place_id`,
						r.cc, null(r.regionCode), null(r.region), null(r.city))
				}
				db.Exec(t, `INSERT INTO calculated_breakdowns(build_id,result_id,dimension,position,ips,observations,place_id,
 asn_label_id,completed,successes,p50) VALUES ($1,$2,$3,$4,$5,$5,$6,$7,$8,$8,$9)`,
					build, result, rankDimensionCode[dimension], i, r.ips, place, label, r.successes, r.p50)
			}
		}
	}
}

// directTileRanksSQL ranks every window's coverage counts above zero across
// networks with RANK(), from the published payload: its networks (v4) or
// its product groups.
const directTileRanksSQL = `WITH v AS (
 SELECT p->>'provider' AS provider, w.key AS range, t.name,
  CASE WHEN (w.value->'pool'->>t.name)::float > 0 THEN (w.value->'pool'->>t.name)::float END AS value
 FROM calculated_snapshots s, jsonb_array_elements(coalesce(s.payload->'networks', s.payload->'products')) p, jsonb_each(p->'windows') w,
  unnest(ARRAY['unique_ips', 'countries', 'regions', 'cities', 'asns']) t(name)
 WHERE s.build_id = $1)
SELECT provider, range, name,
 CASE WHEN value IS NOT NULL THEN rank() OVER (PARTITION BY range, name ORDER BY value DESC NULLS LAST) END,
 count(value) OVER (PARTITION BY range, name)
FROM v`

func sameDBRank(got model.Rank, rank *int64, peers int64) bool {
	if rank == nil {
		return math.IsNaN(float64(got.Rank)) && int64(got.Peers) == peers
	}
	return float64(got.Rank) == float64(*rank) && int64(got.Peers) == peers
}

// The ranks are the same read from a v4 build's network lists, and, before
// v4, from calculated v8's breakdown partition and from v9's lists.
func TestRanksMatchADirectSQLRanking(t *testing.T) {
	t.Parallel()
	t.Run("network lists", func(t *testing.T) {
		db := testdb.New(t)
		ranksMatchSQL(t, db, rankedSnapshot(t, db, true))
	})
	for _, k2 := range []bool{false, true} {
		t.Run(map[bool]string{false: "calculated_breakdowns", true: "lists"}[k2], func(t *testing.T) {
			db := testdb.New(t)
			build := rankedSnapshot(t, db, false)
			if k2 {
				db.MoveToLists(t)
			}
			ranksMatchSQL(t, db, build)
		})
	}
}

func ranksMatchSQL(t *testing.T, db *testdb.DB, build int64) {
	res := buildFrom(t, db)
	ctx := context.Background()
	c := db.Conn(t)
	defer c.Close(ctx)

	rows, err := c.Query(ctx, directTileRanksSQL, build)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	tiles := 0
	for rows.Next() {
		var provider, rangeID, name string
		var peers int64
		var rank *int64
		if err := rows.Scan(&provider, &rangeID, &name, &rank, &peers); err != nil {
			t.Fatal(err)
		}
		tr := res.Details[provider].Provider.TileRanks["residential"][rangeID]
		got := map[string]model.Rank{"unique_ips": tr.UniqueIps, "countries": tr.Countries, "regions": tr.Regions,
			"cities": tr.Cities, "asns": tr.Asns}[name]
		if !sameDBRank(got, rank, peers) {
			t.Errorf("%s %s %s: built %+v, SQL %v/%d", provider, rangeID, name, got, deref(rank), peers)
		}
		tiles++
	}
	if tiles != 4*3*5 {
		t.Errorf("%d tile ranks checked, want 60", tiles)
	}
}

func deref(v *int64) any {
	if v == nil {
		return nil
	}
	return *v
}
