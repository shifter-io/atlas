package store_test

import (
	"context"
	"reflect"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// networkLists adds three network lists (ids 1, 2, 3) to emptyBuild's
// database, each with rows in every dimension (country x ASN too, which a
// network list never has and NetLists does not read), NULL counters included.
func networkLists(t *testing.T) (*testdb.DB, *pgxpool.Pool) {
	t.Helper()
	db := testdb.New(t)
	emptyBuild(t, db)
	db.Exec(t, `INSERT INTO place_names(country_code,country) VALUES ('DE','Germany'),('US','United States');
 INSERT INTO place_names(country_code,country,region_code,region) VALUES ('US','United States','CA','California');
 INSERT INTO place_names(country_code,country,region_code,region,city) VALUES ('US','United States','CA','California','San Jose');
 INSERT INTO asn_names(asn,name) VALUES (64512,'United Transit'), (NULL,'Unnamed');
 INSERT INTO asn_labels(asn_name_id,usage_type) SELECT asn_name_id,'hosting' FROM asn_names WHERE asn = 64512 OR asn IS NULL;
 INSERT INTO calculated_lists(`+testdb.ListColumns+`) SELECT 1,'network',h,`+testdb.ListFill+` FROM unnest('{24,168,720}'::int[]) h;
 INSERT INTO calculated_list_rows(list_id,dimension,position,place_id,asn_label_id,ips,observations,completed,successes,challenges,blocks,p50,p90,p99)
 SELECT l.list_id, v.d::"char", v.pos,
  (SELECT place_id FROM place_names p WHERE p.country_code = v.cc AND p.region IS NOT DISTINCT FROM v.region AND p.city IS NOT DISTINCT FROM v.city),
  (SELECT asn_label_id FROM asn_label_names a WHERE a.asn IS NOT DISTINCT FROM v.asn AND a.name = v.asn_name),
  v.ips * l.list_id, v.ips + 1, v.completed, v.successes, 0, NULL, v.p50, 900, 990
 FROM calculated_lists l, (VALUES
  ('c', 0, 'US', NULL, NULL, NULL::bigint, NULL, 40, 100, 90, 120),
  ('c', 1, 'DE', NULL, NULL, NULL, NULL, 40, NULL, NULL, NULL),
  ('r', 0, 'US', 'California', NULL, NULL, NULL, 30, 50, 45, 80),
  ('g', 0, 'DE', NULL, 'Berlin', NULL, NULL, 25, 10, 8, NULL),
  ('g', 1, 'US', 'California', 'San Jose', NULL, NULL, 20, 30, 29, 70),
  ('a', 0, NULL, NULL, NULL, 64512, 'United Transit', 50, 60, 55, 90),
  ('a', 1, NULL, NULL, NULL, 64496, 'Example', 50, 5, 5, 40),
  ('a', 2, NULL, NULL, NULL, NULL, 'Unnamed', 1, NULL, NULL, NULL),
  ('x', 0, 'US', NULL, NULL, 64512, 'United Transit', 1, 1, 1, 1),
  ('y', 0, 'US', 'California', 'San Jose', 64512, 'United Transit', 20, 30, 29, 70),
  ('y', 1, 'DE', NULL, 'Berlin', 64496, 'Example', 25, 10, 8, NULL)
 ) v(d, pos, cc, region, city, asn, asn_name, ips, completed, successes, p50)`)
	pool, err := store.OpenReadOnly(context.Background(), db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	return db, pool
}

// within runs f in a read-only transaction.
func within(t *testing.T, pool *pgxpool.Pool, f func(tx pgx.Tx)) {
	t.Helper()
	ctx := context.Background()
	tx, err := pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	f(tx)
}

func loadNet(t *testing.T, n *store.NetLists, tx pgx.Tx, ids ...int64) map[int64]*store.NetList {
	t.Helper()
	got, err := n.Load(context.Background(), tx, ids)
	if err != nil {
		t.Fatal(err)
	}
	return got
}

// The rows of a network list read through NetLists equal those read through
// Lists, field by field, in every dimension it has.
func TestNetListsReadTheListRows(t *testing.T) {
	_, pool := networkLists(t)
	within(t, pool, func(tx pgx.Tx) {
		ids := []int64{1, 2, 3}
		full, err := store.FetchK2(context.Background(), store.NewLists(), tx, ids)
		if err != nil {
			t.Fatal(err)
		}
		net := loadNet(t, store.NewNetLists(), tx, ids...)
		for _, id := range ids {
			for _, name := range []string{"country", "region", "city", "asn", "city_asn"} {
				want, l := full[id].Rows(name), net[id]
				if len(want) == 0 || l.Len(name) != len(want) {
					t.Fatalf("list %d %s: %d rows, want %d", id, name, l.Len(name), len(want))
				}
				for i, w := range want {
					g := l.Row(name, i)
					if !reflect.DeepEqual(g.Place, w.Place) || !reflect.DeepEqual(g.Network, w.Network) ||
						g.Ips != w.Ips || g.Completed != w.Completed || g.Successes != w.Successes || g.P50 != w.P50 {
						t.Errorf("list %d %s row %d: %+v, want %+v", id, name, i, g, w)
					}
				}
			}
			if l := net[id]; l.Len("country_asn") != 0 || l.Row("asn", 2).Network.Asn != nil || l.Row("country", 1).Completed != -1 {
				t.Errorf("list %d: country x ASN %d rows, NULL ASN %v", id, l.Len("country_asn"), l.Row("asn", 2).Network.Asn)
			}
		}
		if m := net[2].Match("city_asn", "united st", net[2].ByIps("city_asn")); len(m) != 1 || *net[2].Row("city_asn", int(m[0])).Place.City != "San Jose" {
			t.Errorf("united st: %v", m)
		}
	})
}

// A second load reads only the new id and drops the lists it no longer names;
// a list dropped and named again is read again.
func TestNetListsReadOnlyNewLists(t *testing.T) {
	_, pool := networkLists(t)
	n := store.NewNetLists()
	within(t, pool, func(tx pgx.Tx) {
		first := loadNet(t, n, tx, 1, 2, 2)
		if n.Fetched != 2 || len(first) != 2 {
			t.Fatalf("first load: %d read, %d lists", n.Fetched, len(first))
		}
		second := loadNet(t, n, tx, 2, 3)
		if n.Fetched != 3 || len(second) != 2 || second[2] != first[2] || second[1] != nil {
			t.Errorf("second load: %d read, lists %v", n.Fetched, second)
		}
		if loadNet(t, n, tx, 1, 3); n.Fetched != 4 {
			t.Errorf("a dropped list named again: %d read, want 4", n.Fetched)
		}
		// Names are shared across loads: list 1's places are list 3's.
		again := loadNet(t, n, tx, 1, 3)
		if again[1].Row("city", 0).Place != again[3].Row("city", 0).Place || n.Fetched != 4 {
			t.Errorf("names not shared, or %d read", n.Fetched)
		}
	})
}

// A failed load keeps nothing: the next one reads every list again.
func TestNetListsFailedLoadEmpties(t *testing.T) {
	db, pool := networkLists(t)
	n := store.NewNetLists()
	within(t, pool, func(tx pgx.Tx) { loadNet(t, n, tx, 1) })
	within(t, pool, func(tx pgx.Tx) {
		tx.Rollback(context.Background()) //nolint:errcheck
		if _, err := n.Load(context.Background(), tx, []int64{1, 2}); err == nil {
			t.Fatal("a load in a closed transaction succeeded")
		}
	})
	within(t, pool, func(tx pgx.Tx) {
		if loadNet(t, n, tx, 1); n.Fetched != 3 {
			t.Errorf("after a failed load %d read, want list 1 read again (3)", n.Fetched)
		}
	})
	db.Exec(t, `UPDATE calculated_list_rows SET ips = 4294967295 WHERE list_id = 3 AND dimension = 'c' AND position = 0`)
	within(t, pool, func(tx pgx.Tx) {
		_, err := n.Load(context.Background(), tx, []int64{3})
		if err == nil || !strings.Contains(err.Error(), "list 3: counter 4294967295 does not fit") {
			t.Errorf("a counter past the compact row: %v", err)
		}
	})
}
