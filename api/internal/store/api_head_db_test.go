package store_test

import (
	"bytes"
	"context"
	"encoding/json"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
)

// apiHeadSet reports whether the current build has an API head.
func apiHeadSet(t *testing.T, db *testdb.DB) bool {
	t.Helper()
	c := db.Conn(t)
	defer c.Close(context.Background())
	var set bool
	if err := c.QueryRow(context.Background(), "SELECT api_head IS NOT NULL FROM calculated_snapshots JOIN current_calculation USING (build_id)").Scan(&set); err != nil {
		t.Fatal(err)
	}
	return set
}

// buildDetail is the build of in's answer for provider slug, as JSON.
func buildDetail(t *testing.T, in *store.Inputs, slug string) []byte {
	t.Helper()
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	out, err := json.Marshal(res.Details[slug])
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// One v4 build read both ways (API-LOAD-STATEMENTS-1): from the API head it
// was published with (calculated v14), and, that head removed, built from
// its head as before v14. Both reads give the same head, byte for byte, and
// the same answers.
func TestAV4BuildReadsTheSameFromItsAPIHead(t *testing.T) {
	t.Parallel()
	db, _ := networkLists(t)
	db.Publish(t, v4Head(t, 50))
	if !apiHeadSet(t, db) {
		t.Fatal("publishing a v4 build wrote no API head")
	}
	stored := load(t, db)
	db.Exec(t, "UPDATE calculated_snapshots SET api_head = NULL")
	built := load(t, db)
	if !bytes.Equal(stored.Snapshot.Payload, built.Snapshot.Payload) || stored.Snapshot.BuildID != built.Snapshot.BuildID {
		t.Fatalf("the API head read differs from the head built:\n%.600s\n%.600s", stored.Snapshot.Payload, built.Snapshot.Payload)
	}
	if a, b := buildDetail(t, stored, "shifter"), buildDetail(t, built, "shifter"); !bytes.Equal(a, b) {
		t.Errorf("shifter's answer differs:\n%.600s\n%.600s", a, b)
	}
}

// The API reads a build's API head as it is stored, without reading its
// payload: a payload that is no v4 head any more still serves the head.
func TestTheAPIHeadIsReadWithoutThePayload(t *testing.T) {
	t.Parallel()
	db, _ := networkLists(t)
	db.Publish(t, v4Head(t, 0))
	want := load(t, db).Snapshot.Payload
	db.Exec(t, `UPDATE calculated_snapshots SET payload = '{}'`)
	if got := load(t, db).Snapshot.Payload; !bytes.Equal(got, want) {
		t.Errorf("the head read is %.300s, want the stored API head %.300s", got, want)
	}
}

// A head before v4 gets no API head when it is published.
func TestAHeadBeforeV4HasNoAPIHead(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	emptyBuild(t, db)
	if apiHeadSet(t, db) {
		t.Error("a head before v4 has an API head")
	}
}
