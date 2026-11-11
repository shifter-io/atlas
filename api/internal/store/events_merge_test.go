package store_test

import (
	"context"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
)

// An outage seen by the generic_http and generic_https pulses of the same
// slots counts once (EVENTS-MERGE-1): one down and one recovered, though the
// two targets' rows are a minute apart (the down's in two 30-minute buckets);
// the routine pulses stay one per target, and an incident without a target
// (a no_node, twice at one time) is not merged. Each row reads its slot.
func TestTheCountsTakeAnIncidentOnTwoTargetsOnce(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	ctx := context.Background()
	db.Exec(t, "INSERT INTO providers VALUES ('a')")
	db.Exec(t, "INSERT INTO products(provider,name) VALUES ('a','residential')")
	db.Exec(t, `INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,start_at)
 SELECT 'pulse','DE',1,t,date_bin('30 minutes', now(), '2000-01-01') - s * interval '30 minutes',now()
 FROM unnest(ARRAY['generic_http','generic_https']) t, generate_series(1, 2) s`)
	// The 1 h ago slot: down at 61 and 59 minutes ago, a pulse per target;
	// the 30 min ago slot: recovered at 31 and 30 minutes ago.
	db.Exec(t, `INSERT INTO test_events(at,kind,batch_id,product_id,country_code,target_id,region_id,signal)
 SELECT now() - m * interval '1 minute', k, b.batch_id, p.product_id, 'DE', b.target_id, 1, sig
 FROM (VALUES ('down',2,'generic_http',61,-1),('down',2,'generic_https',59,-1),('pulse_finished',2,'generic_http',58,0),
 ('pulse_finished',2,'generic_https',58,0),('recovered',1,'generic_http',31,1),('recovered',1,'generic_https',30,1))
 v(k,s,t,m,sig) JOIN test_batches b ON b.target_id = v.t
 AND b.scheduled_for = date_bin('30 minutes', now(), '2000-01-01') - v.s * interval '30 minutes', products p`)
	db.Exec(t, `INSERT INTO test_events(at,kind,product_id,signal) SELECT now() - interval '10 minutes','no_node',product_id,-1
 FROM products, generate_series(1, 2)`)
	c := db.Conn(t)
	defer c.Close(ctx)
	in, err := store.LoadEvents(ctx, c)
	if err != nil {
		t.Fatal(err)
	}
	counts, good, bad := map[string]int{}, 0, 0
	for _, b := range in.EventStats.Buckets {
		counts[b.Category] += b.Events
		good, bad = good+b.Good, bad+b.Bad
	}
	if counts["incident"] != 4 || counts["pulse"] != 2 || good != 1 || bad != 3 {
		t.Errorf("counts %v, good %d, bad %d", counts, good, bad)
	}
	slots := 0
	for _, e := range in.Events {
		if e.Slot != nil && e.Slot.Before(time.Now()) {
			slots++
		}
	}
	if slots != 6 {
		t.Errorf("%d of the batches' 6 rows read their slot", slots)
	}
}
