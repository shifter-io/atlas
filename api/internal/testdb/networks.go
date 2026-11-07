package testdb

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
	"github.com/jackc/pgx/v5"
)

// PublishV4 publishes a snapshot payload before v4 (v1 or v2, as Publish
// takes) as the payload v4 build of the same data, as the current published
// calculation: one network per product group (testscale.NetworkHead), the
// head with its networks in calculated_snapshots and each network window's
// list in calculated_lists and calculated_list_rows. The networks' peers are
// the payload's overlap rows. Returns the build id. Its answers are those of
// Publish(testscale.Listed(payload)) with testscale.AsV4Answers applied.
func (d *DB) PublishV4(t testing.TB, payload []byte) int64 {
	t.Helper()
	ctx := context.Background()
	c := d.Conn(t)
	defer c.Close(ctx)
	var s struct {
		Products []json.RawMessage `json:"products"`
	}
	if err := json.Unmarshal(payload, &s); err != nil {
		t.Fatal(err)
	}
	id := d.register(t, c, payload, s.Products)
	head, err := testscale.NetworkHead(&store.SnapshotRow{BuildID: id, Payload: payload}, 0,
		func(w testscale.NetWindow, rows map[string][]store.NetRow) (int64, map[string]int, error) {
			return writeNetList(ctx, c, id, Product(t, c, w.Provider, w.Product), w.Hours, rows)
		})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.Exec(ctx, "INSERT INTO calculated_snapshots VALUES ($1,$2,$3)", id, "test-"+hex.EncodeToString(randomBytes()), head); err != nil {
		t.Fatal(err)
	}
	current(t, c, id)
	return id
}

// netListRowsSQL writes list $1's rows from the jsonb array $2 of breakdown
// rows (testscale.BreakdownRows) with their dimension code (d) and position
// (pos), adding the place and ASN names they need. A network list keeps no
// observations (0), challenges, blocks, p90 or p99 (NULL).
const netListRowsSQL = `WITH r AS (SELECT *, d <> 'a' AS place, d IN ('a','y') AS network FROM jsonb_to_recordset($2) AS r(d text, pos int,
 country_code text, country text, region_code text, region text, city text, asn bigint, name text, usage_type text,
 ips bigint, completed bigint, successes bigint, p50 int)),
p AS (INSERT INTO place_names(country_code,country,region_code,region,city)
 SELECT DISTINCT country_code,country,region_code,region,city FROM r WHERE place
 ON CONFLICT (country_code,country,region_code,region,city) DO UPDATE SET city=excluded.city RETURNING *),
n AS (INSERT INTO asn_names(asn,name) SELECT DISTINCT asn,name FROM r WHERE network
 ON CONFLICT (asn,name) DO UPDATE SET name=excluded.name RETURNING *),
l AS (INSERT INTO asn_labels(asn_name_id,usage_type)
 SELECT DISTINCT n.asn_name_id,r.usage_type FROM r JOIN n ON r.network AND (n.asn,n.name) IS NOT DISTINCT FROM (r.asn,r.name)
 ON CONFLICT (asn_name_id,usage_type) DO UPDATE SET usage_type=excluded.usage_type RETURNING *)
INSERT INTO calculated_list_rows
SELECT $1,r.d::"char",r.pos,p.place_id,l.asn_label_id,coalesce(r.ips,0),0,r.completed,r.successes,NULL,NULL,r.p50,NULL,NULL FROM r
 LEFT JOIN p ON r.place AND (p.country_code,p.country,p.region_code,p.region,p.city)
  IS NOT DISTINCT FROM (r.country_code,r.country,r.region_code,r.region,r.city)
 LEFT JOIN n ON r.network AND (n.asn,n.name) IS NOT DISTINCT FROM (r.asn,r.name)
 LEFT JOIN l ON l.asn_name_id=n.asn_name_id AND l.usage_type IS NOT DISTINCT FROM r.usage_type`

// netListCodes are the list row dimensions of the breakdown names.
var netListCodes = map[string]string{"country": "c", "region": "r", "city": "g", "asn": "a", "city_asn": "y"}

// writeNetList stores a network list of product's window of hours, created
// by build, with rows by breakdown name, and returns its id and row counts.
func writeNetList(ctx context.Context, c *pgx.Conn, build, product, hours int64, rows map[string][]store.NetRow) (int64, map[string]int, error) {
	var id int64
	err := c.QueryRow(ctx, "INSERT INTO calculated_lists("+ListColumns+") VALUES ($1,$2,$3,'test','{}','{}',$4) RETURNING list_id",
		product, store.NetworkCohort, hours, build).Scan(&id)
	if err != nil {
		return 0, nil, err
	}
	out := []map[string]any{}
	for name, rs := range testscale.BreakdownRows(rows) {
		for i, r := range rs {
			r["d"], r["pos"] = netListCodes[name], i
			out = append(out, r)
		}
	}
	b, err := json.Marshal(out)
	if err == nil {
		_, err = c.Exec(ctx, netListRowsSQL, id, b)
	}
	return id, testscale.RowCounts(rows), err
}
