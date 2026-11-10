package build_test

import (
	"bytes"
	"context"
	"encoding/json"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
)

// A build reads no list row from the database (SITEAPI-REBUILD-1): its load
// read every row of its network lists once, so a build whose rows were
// deleted after its load gives the answers a build gives before.
func TestTheBuildReadsTheRowsItsLoadRead(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	c := db.Conn(t)
	defer c.Close(context.Background())
	nc := writeNetLists(t, c)
	tailLists(t, c, nc)
	v4, groups := netPayloads(t, c, nc)
	db.PublishParts(t, v4, groups, nil)
	ctx := context.Background()
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	load := func() *store.Inputs {
		in, err := store.LoadLists(ctx, pool, store.NewLists())
		if err != nil {
			t.Fatal(err)
		}
		return in
	}
	before, after := load(), load()
	want, err := build.Build(before)
	if err != nil {
		t.Fatal(err)
	}
	db.Exec(t, `DELETE FROM calculated_list_rows`)
	got, err := build.Build(after)
	if err != nil {
		t.Fatalf("a build after its rows were deleted: %v", err)
	}
	for name, pair := range map[string][2]any{"networks": {want.Pages.Network, got.Pages.Network},
		"countries": {want.Pages.Country, got.Pages.Country}, "lists": {want.Dataset.Lists, got.Dataset.Lists}} {
		w, _ := json.Marshal(pair[0])
		g, _ := json.Marshal(pair[1])
		if len(w) < 100 || !bytes.Equal(g, w) {
			t.Errorf("%s: %d bytes, want the %d bytes built before", name, len(g), len(w))
		}
	}
}
