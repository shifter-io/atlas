package testdb

import (
	"context"
	"testing"
	"time"
)

// Settle waits until every transaction that committed before the call is
// older than the oldest transaction running in the cluster, so the rows they
// wrote read as settled (store.Event.Unsettled is false). Transaction ids are
// the cluster's, so another test's open transaction holds it back; it fails
// the test after a minute.
func (d *DB) Settle(t testing.TB) {
	t.Helper()
	ctx := context.Background()
	c := d.Conn(t)
	defer c.Close(ctx)
	var mark string
	if err := c.QueryRow(ctx, "SELECT pg_snapshot_xmax(pg_current_snapshot())::text").Scan(&mark); err != nil {
		t.Fatal(err)
	}
	for deadline := time.Now().Add(time.Minute); ; time.Sleep(20 * time.Millisecond) {
		var settled bool
		if err := c.QueryRow(ctx, "SELECT pg_snapshot_xmin(pg_current_snapshot()) >= $1::xid8", mark).Scan(&settled); err != nil {
			t.Fatal(err)
		}
		if settled {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("transactions before xid %s still running after a minute", mark)
		}
	}
}
