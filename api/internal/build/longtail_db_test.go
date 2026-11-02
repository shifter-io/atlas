package build_test

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"reflect"
	"testing"

	"github.com/Zeus-Development/proxy-index-website/api/internal/testdb"
)

// cityAsnRows are city x ASN rows in the published row shape (calculated_breakdown_details).
func cityAsnRows() []any {
	var rows []any
	for i, city := range []string{"Berlin", "Hamburg", "Munich", "Cologne"} {
		rows = append(rows, map[string]any{
			"country_code": "DE", "country": "Germany", "region_code": fmt.Sprint("R", i), "region": city, "city": city,
			"asn": 3320 + i, "name": fmt.Sprint("Network ", i), "usage_type": "residential",
			"ips": 40 - i, "observations": 50 - i, "completed": 20, "successes": 19, "success_rate": 0.95,
			"challenges": 1, "blocks": 0, "p50": 50 + i, "p90": 100, "p99": 200,
		})
	}
	return rows
}

// snapshotVariants returns the every-request fixture with four city x ASN rows per window,
// whole and capped the way proxy-index's slim() caps it (city x ASN to 1 row, ASNs to 2).
func snapshotVariants(t *testing.T) (whole, capped []byte, products []map[string]any) {
	t.Helper()
	raw, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil {
		t.Fatal(err)
	}
	variant := func(cityAsn, asns int) []byte {
		var snapshot map[string]any
		if err := json.Unmarshal(raw, &snapshot); err != nil {
			t.Fatal(err)
		}
		for _, entry := range snapshot["products"].([]any) {
			for _, entry := range entry.(map[string]any)["windows"].(map[string]any) {
				window := entry.(map[string]any)
				lists, pool := window["breakdowns"].(map[string]any), window["pool"].(map[string]any)
				lists["city_asn"] = cityAsnRows()[:cityAsn]
				window["breakdown_entries"] = map[string]any{"country": len(lists["country"].([]any)), "city_asn": 4}
				pool["list_entries"] = map[string]any{"geo": len(pool["geo"].([]any)),
					"countries_breakdown": len(pool["countries_breakdown"].([]any)), "asns_breakdown": len(pool["asns_breakdown"].([]any))}
				pool["asns_breakdown"] = pool["asns_breakdown"].([]any)[:asns]
			}
		}
		b, err := json.Marshal(snapshot)
		if err != nil {
			t.Fatal(err)
		}
		return b
	}
	whole, capped = variant(4, 12), variant(1, 2)
	var doc struct{ Products []map[string]any }
	if err := json.Unmarshal(whole, &doc); err != nil {
		t.Fatal(err)
	}
	return whole, capped, doc.Products
}

// storeTypedRows writes the typed breakdown rows calculate() persists for the whole lists:
// city x ASN rows by position and ASN rows in reverse IP order, plus an ASN without IPs, in
// the build's partition with the shared place and ASN names (proxy-index calculated/schema.sql).
func storeTypedRows(t *testing.T, db *testdb.DB, buildID int64, products []map[string]any) {
	ctx := context.Background()
	c := db.Conn(t)
	defer c.Close(ctx)
	ids := map[string]int64{}
	id := func(sql string, args ...any) int64 {
		key := fmt.Sprint(sql, args)
		if _, ok := ids[key]; !ok {
			var v int64
			if err := c.QueryRow(ctx, sql, args...).Scan(&v); err != nil {
				t.Fatal(err)
			}
			ids[key] = v
		}
		return ids[key]
	}
	asn := func(r map[string]any) int64 {
		name := id("INSERT INTO asn_names(asn,name) VALUES ($1,$2) RETURNING asn_name_id", int64(r["asn"].(float64)), r["name"])
		return id("INSERT INTO asn_labels(asn_name_id,usage_type) VALUES ($1,$2) RETURNING asn_label_id", name, r["usage_type"])
	}
	insert := func(result int64, dimension string, position int, r map[string]any, place any, counters ...any) {
		args := append([]any{buildID, result, dimension, position, int64(r["ips"].(float64)), int64(r["observations"].(float64)), place, asn(r)}, counters...)
		for len(args) < 15 {
			args = append(args, nil)
		}
		db.Exec(t, `INSERT INTO calculated_breakdowns(build_id,result_id,dimension,position,ips,observations,place_id,asn_label_id,
 completed,successes,challenges,blocks,p50,p90,p99) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)`, args...)
	}
	db.Exec(t, fmt.Sprintf("CREATE TABLE calculated_breakdowns_b%d PARTITION OF calculated_breakdowns FOR VALUES IN (%d)", buildID, buildID))
	for _, p := range products {
		product := id("SELECT product_id FROM products WHERE provider=$1 AND name=$2", p["provider"], p["product"])
		db.Exec(t, "INSERT INTO cohorts(cohort) VALUES ($1) ON CONFLICT DO NOTHING", p["cohort"])
		for _, entry := range p["windows"].(map[string]any) {
			window := entry.(map[string]any)
			hours := int64(window["period"].(map[string]any)["hours"].(float64))
			result := id(`INSERT INTO calculated_windows(build_id,product_id,cohort,target,window_hours,completed,successes,errors,
 body_bytes,coverage,readiness) VALUES ($1,$2,$3,'',$4,0,0,0,0,0,'ready') RETURNING result_id`, buildID, product, p["cohort"], hours)
			for i, entry := range window["breakdowns"].(map[string]any)["city_asn"].([]any) {
				r := entry.(map[string]any)
				place := id("INSERT INTO place_names(country_code,country,region_code,region,city) VALUES ($1,$2,$3,$4,$5) RETURNING place_id",
					r["country_code"], r["country"], r["region_code"], r["region"], r["city"])
				var counters []any
				for _, k := range []string{"completed", "successes", "challenges", "blocks", "p50", "p90", "p99"} {
					counters = append(counters, int64(r[k].(float64)))
				}
				insert(result, "y", i, r, place, counters...)
			}
			asns := window["pool"].(map[string]any)["asns_breakdown"].([]any)
			for i := range asns {
				insert(result, "a", i, asns[len(asns)-1-i].(map[string]any), nil)
			}
			insert(result, "a", len(asns), map[string]any{"asn": 1.0, "name": "Idle", "usage_type": "hosting", "ips": 0.0, "observations": 3.0}, nil)
		}
	}
}

func TestCappedSnapshotServesTheSameProviderAsTheWholeOne(t *testing.T) {
	whole, capped, products := snapshotVariants(t)
	expected := measuredOnly(t, whole)
	db := testdb.New(t)
	storeTypedRows(t, db, db.Publish(t, capped), products)
	res := buildFrom(t, db)
	want, got := generic(t, expected.Details["shifter"]), generic(t, res.Details["shifter"])
	if !reflect.DeepEqual(want, got) {
		var diffs []string
		diff(want, got, "shifter", &diffs, map[string]bool{})
		t.Fatalf("the capped snapshot changed the provider:\n%v", diffs)
	}
	// /v1/snapshot stays the published (capped) snapshot.
	var served struct {
		Products []struct {
			Windows map[string]struct {
				Breakdowns map[string][]any `json:"breakdowns"`
			} `json:"windows"`
		} `json:"products"`
	}
	if err := json.Unmarshal(res.SnapshotPayload, &served); err != nil {
		t.Fatal(err)
	}
	if n := len(served.Products[0].Windows["30d"].Breakdowns["city_asn"]); n != 1 {
		t.Errorf("served snapshot has %d city x ASN rows, want the published 1", n)
	}
}
