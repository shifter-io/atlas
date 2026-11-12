package testdb

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
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
	return d.PublishV4With(t, payload, V4Lists{})
}

// V4Lists is how PublishV4With publishes a payload's networks.
type V4Lists struct {
	// Requested gives every network that many requested countries
	// (testscale.NetworkHead); 0 gives each its group's own.
	Requested int
	// Rows, when set, is every network window's list instead of the
	// payload's: written once, then copied for each other window over
	// several connections at once. Its lists are not ranked (ips_rank.sql):
	// the week's copies of one list are timed and sized, not ranked, and
	// ranking millions of rows at every publish took a minute and more.
	Rows map[string][]store.NetRow
	// Small, when set, is the list of every window of the network of
	// provider SmallProvider instead: a network smaller than the others.
	Small         map[string][]store.NetRow
	SmallProvider string
}

// PublishV4With is PublishV4 with the networks as o has them.
func (d *DB) PublishV4With(t testing.TB, payload []byte, o V4Lists) int64 {
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
	var first int64
	var copies []int64
	ranks := netRanks{kinds: productKinds(s.Products)}
	head, err := testscale.NetworkHead(&store.SnapshotRow{BuildID: id, Payload: payload}, o.Requested,
		func(w testscale.NetWindow, rows map[string][]store.NetRow) (list int64, n map[string]int, err error) {
			product := Product(t, c, w.Provider, w.Product)
			defer func() { ranks.add(list, w) }()
			if o.Small != nil && w.Provider == o.SmallProvider {
				return writeNetList(ctx, c, id, product, w.Hours, o.Small)
			}
			if o.Rows != nil && first != 0 {
				list, err = newNetList(ctx, c, id, product, w.Hours)
				copies = append(copies, list)
				return list, testscale.RowCounts(o.Rows), err
			}
			if o.Rows != nil {
				rows = o.Rows
			}
			list, n, err = writeNetList(ctx, c, id, product, w.Hours, rows)
			first = list
			return list, n, err
		})
	if err == nil {
		err = d.copyNetLists(ctx, first, copies)
	}
	if err == nil && o.Rows == nil {
		err = ranks.write(ctx, c)
	}
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.Exec(ctx, "INSERT INTO calculated_snapshots VALUES ($1,$2,$3)", id, "test-"+hex.EncodeToString(randomBytes()), head); err != nil {
		t.Fatal(err)
	}
	current(t, c, id)
	return id
}

// Republish publishes the current build's head again as a new build, edited
// by edit (its as-of an hour later, say). Its network windows name the same
// lists, as a build that reuses every list does. It returns the build id.
func (d *DB) Republish(t testing.TB, edit func(head []byte) []byte) int64 {
	t.Helper()
	ctx := context.Background()
	c := d.Conn(t)
	defer c.Close(ctx)
	var head []byte
	if err := c.QueryRow(ctx, "SELECT s.payload::text FROM calculated_snapshots s JOIN current_calculation USING (build_id)").Scan(&head); err != nil {
		t.Fatal(err)
	}
	head = edit(head)
	id := d.register(t, c, head, nil)
	if _, err := c.Exec(ctx, "INSERT INTO calculated_snapshots VALUES ($1,$2,$3)", id, "test-"+hex.EncodeToString(randomBytes()), head); err != nil {
		t.Fatal(err)
	}
	current(t, c, id)
	return id
}

// copyNetLists gives each list of to list from's rows, from several
// connections at once. The copies skip the foreign key checks (replica
// mode): their names are those of rows that passed them.
func (d *DB) copyNetLists(ctx context.Context, from int64, to []int64) error {
	return d.inParallel(ctx, len(to), func(c *pgx.Conn, i int) error {
		_, err := c.Exec(ctx, "SET session_replication_role = replica")
		if err == nil {
			_, err = c.Exec(ctx, "INSERT INTO calculated_list_rows SELECT $2,dimension,position,place_id,asn_label_id,ips,observations,"+
				"completed,successes,challenges,blocks,p50,p90,p99,ips_rank,ips_peers FROM calculated_list_rows WHERE list_id=$1", from, to[i])
		}
		return err
	})
}

// netListRowsSQL writes list $1's rows from the jsonb array $2 of breakdown
// rows (testscale.BreakdownRows) with their dimension code (d) and position
// (pos), adding the place and ASN names they need. A network list keeps no
// observations (0), challenges, blocks, p90 or p99 (NULL). A row finds its names
// by a jsonb array of their columns, which equals when each column is the
// same or both are NULL (as IS NOT DISTINCT FROM) and, unlike it, hashes: a
// QA-1-sized list joins in a hash join, not a nested loop over every name.
const netListRowsSQL = `WITH r AS (SELECT *, d <> 'a' AS place, d IN ('a','y') AS network FROM jsonb_to_recordset($2) AS r(d text, pos int,
 country_code text, country text, region_code text, region text, city text, asn bigint, name text, usage_type text,
 ips bigint, completed bigint, successes bigint, p50 int)),
p AS (INSERT INTO place_names(country_code,country,region_code,region,city)
 SELECT DISTINCT country_code,country,region_code,region,city FROM r WHERE place
 ON CONFLICT (country_code,country,region_code,region,city) DO UPDATE SET city=excluded.city RETURNING *),
n AS (INSERT INTO asn_names(asn,name) SELECT DISTINCT asn,name FROM r WHERE network
 ON CONFLICT (asn,name) DO UPDATE SET name=excluded.name RETURNING *),
l AS (INSERT INTO asn_labels(asn_name_id,usage_type)
 SELECT DISTINCT n.asn_name_id,r.usage_type FROM r JOIN n ON r.network AND jsonb_build_array(n.asn,n.name) = jsonb_build_array(r.asn,r.name)
 ON CONFLICT (asn_name_id,usage_type) DO UPDATE SET usage_type=excluded.usage_type RETURNING *)
INSERT INTO calculated_list_rows
SELECT $1,r.d::"char",r.pos,p.place_id,l.asn_label_id,coalesce(r.ips,0),0,r.completed,r.successes,NULL,NULL,r.p50,NULL,NULL FROM r
 LEFT JOIN p ON r.place AND jsonb_build_array(p.country_code,p.country,p.region_code,p.region,p.city)
  = jsonb_build_array(r.country_code,r.country,r.region_code,r.region,r.city)
 LEFT JOIN n ON r.network AND jsonb_build_array(n.asn,n.name) = jsonb_build_array(r.asn,r.name)
 LEFT JOIN l ON l.asn_name_id=n.asn_name_id AND l.usage_type IS NOT DISTINCT FROM r.usage_type`

// netListCodes are the list row dimensions of the breakdown names.
var netListCodes = map[string]string{"country": "c", "region": "r", "city": "g", "asn": "a", "city_asn": "y"}

// writeNetList stores a network list of product's window of hours, created
// by build, with rows by breakdown name, and returns its id and row counts.
func writeNetList(ctx context.Context, c *pgx.Conn, build, product, hours int64, rows map[string][]store.NetRow) (int64, map[string]int, error) {
	id, err := newNetList(ctx, c, build, product, hours)
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

// newNetList records a network list of product's window of hours, created by
// build, without rows, and returns its id.
func newNetList(ctx context.Context, c *pgx.Conn, build, product, hours int64) (int64, error) {
	var id int64
	err := c.QueryRow(ctx, "INSERT INTO calculated_lists("+ListColumns+") VALUES ($1,$2,$3,'test','{}','{}',$4) RETURNING list_id",
		product, store.NetworkCohort, hours, build).Scan(&id)
	return id, err
}

// netRanks are the network lists PublishV4With wrote and their network
// kinds, which write ranks as proxy-index calculated v15 does
// (netlists.rank(), ips_rank.sql): each list's IPs at a place among the
// lists of its kind and window.
type netRanks struct {
	kinds map[[2]string]string // provider, product -> network kind
	lists []int64
	of    []string
}

// productKinds is the kind of each payload product, as its network id
// (provider:kind:network) names it.
func productKinds(products []json.RawMessage) map[[2]string]string {
	out := map[[2]string]string{}
	for _, raw := range products {
		var p struct{ Provider, Product, Kind string }
		if json.Unmarshal(raw, &p) == nil {
			out[[2]string{p.Provider, p.Product}] = p.Kind
		}
	}
	return out
}

func (r *netRanks) add(list int64, w testscale.NetWindow) {
	if list != 0 {
		r.lists, r.of = append(r.lists, list), append(r.of, r.kinds[[2]string{w.Provider, w.Product}])
	}
}

// ipsRank is the framework's statement that ranks a build's network lists
// (calculated v15), its two parameters as pgx names them.
var ipsRank = filepath.Join("calculated", "ips_rank.sql")

func (r *netRanks) write(ctx context.Context, c *pgx.Conn) error {
	sql, err := os.ReadFile(filepath.Join(FrameworkDir(), ipsRank))
	if err != nil {
		return err
	}
	query := strings.NewReplacer("%(lists)s", "$1", "%(kinds)s", "$2").Replace(string(sql))
	_, err = c.Exec(ctx, query, r.lists, r.of)
	return err
}
