package build

import (
	"context"
	"io"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/jackc/pgx/v5"
)

// A dataset file whose network list lost its rows (contract K2 deleted it)
// fails, naming the list, instead of writing its lists without them: a place
// dimension's rows and the Coverage rows alike.
func TestADatasetFileFailsWhenAListCannotBeRead(t *testing.T) {
	db := testdb.New(t)
	ctx := context.Background()
	c := db.Conn(t)
	defer c.Close(ctx)
	product := testdb.Product(t, c, "p", "residential")
	var id int64
	if err := c.QueryRow(ctx, `INSERT INTO calculated_lists(`+testdb.ListColumns+`) VALUES ($1,'network',720,`+testdb.ListFill+`) RETURNING list_id`,
		product).Scan(&id); err != nil {
		t.Fatal(err)
	}
	db.Exec(t, `INSERT INTO place_names(country_code,country,region_code,region,city) VALUES ('DE','Germany','BE','Berlin','Berlin');
 INSERT INTO asn_names(asn,name) VALUES (64496,'Example'); INSERT INTO asn_labels(asn_name_id,usage_type) SELECT asn_name_id,'isp' FROM asn_names`)
	db.Exec(t, `INSERT INTO calculated_list_rows(list_id,dimension,position,place_id,asn_label_id,ips,observations)
 SELECT $1, d::"char", 0, (SELECT place_id FROM place_names), CASE WHEN d = 'y' THEN (SELECT asn_label_id FROM asn_labels) END, 5, 5
 FROM unnest('{g,y}'::text[]) d`, id)
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	tx, err := pool.BeginTx(ctx, pgx.TxOptions{AccessMode: pgx.ReadOnly})
	if err != nil {
		t.Fatal(err)
	}
	lists, err := store.NewNetLists(pool).Load(ctx, tx, []int64{id})
	tx.Rollback(ctx) //nolint:errcheck
	if err != nil {
		t.Fatal(err)
	}
	l := lists[id]
	db.Exec(t, `DELETE FROM calculated_list_rows WHERE list_id = $1`, id)
	for name, k := range map[string]*datasetKind{
		"city":     {kind: "residential", srcs: [3][]placeSource{{{list: l, scale: 1}}}},
		"coverage": {kind: "residential", coverage: [3]*coverageList{{list: l, scale: 1}}},
	} {
		d := &model.Dataset{Lists: &DatasetLists{networks: []*datasetNetwork{{slug: "p", kinds: []*datasetKind{k}}}}}
		if err := WriteDataset(io.Discard, d); err == nil || !strings.Contains(err.Error(), "network list") || !strings.Contains(err.Error(), "the list was deleted") {
			t.Errorf("%s rows of a deleted list: %v", name, err)
		}
	}
}
