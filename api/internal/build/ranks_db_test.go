package build_test

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"math/rand"
	"os"
	"testing"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/testdb"
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
// to 2 rows the way slim() caps them, as payload v4 (each network its group,
// testdb.PublishV4). Returns the build id.
func rankedSnapshot(t *testing.T, db *testdb.DB) int64 {
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
			for dimension := range rankDimensionCode {
				all := seededPlaces(rng, dimension)
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
	return db.PublishV4(t, payload)
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

// The ranks are the same read from a v4 build's network lists.
func TestRanksMatchADirectSQLRanking(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	ranksMatchSQL(t, db, rankedSnapshot(t, db))
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
