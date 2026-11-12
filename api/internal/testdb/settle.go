package testdb

import (
	"context"
	"testing"
	"time"
)

// Settle waits until every transaction that had an xid when it was called
// has ended, so the rows they wrote read as settled (store.Event.Unsettled is
// false) and a read made after it passes every read made before
// (EV-CURSOR-SETTLE-1: its xmin reaches their Next). Its mark is Next as
// store.eventsReadSQL reads it, not the snapshot's xmax: a transaction
// running with an xid newer than every finished one is at or past xmax, so
// waiting for xmax let a read made before keep the cursor bound down
// (FLAKY-EVENTS-REFRESH-1). Transaction ids are the cluster's, so another
// test's open transaction holds it back; it fails the test after a minute.
func (d *DB) Settle(t testing.TB) {
	t.Helper()
	ctx := context.Background()
	c := d.Conn(t)
	defer c.Close(ctx)
	var mark string
	if err := c.QueryRow(ctx, `SELECT (x::text::bigint + age(xid(x)))::text
 FROM (SELECT pg_snapshot_xmin(pg_current_snapshot()) AS x) s`).Scan(&mark); err != nil {
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
