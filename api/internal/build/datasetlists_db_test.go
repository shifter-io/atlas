package build_test

import (
	"bytes"
	"context"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
)

// The dataset file reads no list row from the database (DATASET-SIZE-2): its
// lists are the first pages the build keeps, so a file written after every
// list row is gone (contract K2 deleted the lists) is the file written
// before, and its lists hold rows.
func TestTheDatasetFileReadsNoListRows(t *testing.T) {
	db := testdb.New(t)
	c := db.Conn(t)
	defer c.Close(context.Background())
	nc := writeNetLists(t, c)
	tailLists(t, c, nc)
	_, v4, groups := netPayloads(t, c, nc)
	db.PublishParts(t, v4, groups, nil)
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
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	var before, after bytes.Buffer
	if err := build.WriteDataset(&before, res.Dataset); err != nil {
		t.Fatal(err)
	}
	db.Exec(t, `DELETE FROM calculated_list_rows`)
	if err := build.WriteDataset(&after, res.Dataset); err != nil || !bytes.Equal(after.Bytes(), before.Bytes()) {
		t.Fatalf("the file written without list rows differs (%v)", err)
	}
	_, lists := readLists(t, after.Bytes())
	if len(lists) == 0 || lists[0].lists["30d"]["city"].total <= build.PageSize {
		t.Fatalf("the file's lists: %+v", lists)
	}
}
