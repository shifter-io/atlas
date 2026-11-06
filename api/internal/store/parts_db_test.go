package store_test

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// text is a query's one text value.
func text(t *testing.T, db *testdb.DB, sql string, args ...any) string {
	t.Helper()
	c := db.Conn(t)
	defer c.Close(context.Background())
	var s string
	if err := c.QueryRow(context.Background(), sql, args...).Scan(&s); err != nil {
		t.Fatalf("%s: %v", sql, err)
	}
	return s
}

func tryLoad(t *testing.T, db *testdb.DB) (*store.Inputs, error) {
	t.Helper()
	ctx := context.Background()
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	return store.Load(ctx, pool)
}

// A build stored in parts, its daily group's 24-hour series sparse, reads as
// the same payload stored whole, byte for byte, without its runs; the whole
// payload still reads as it is.
func TestPartsReadAsTheSinglePayload(t *testing.T) {
	db := testdb.New(t)
	payload, err := testscale.EveryRequest()
	if err != nil {
		t.Fatal(err)
	}
	whole := db.Publish(t, payload)
	in := load(t, db)
	if got, want := string(in.Snapshot.Payload), text(t, db, "SELECT payload::text FROM calculated_snapshots WHERE build_id=$1", whole); got != want {
		t.Fatalf("the single payload reads changed:\n%.300s", got)
	}
	head, products, runs, err := testscale.Parts(payload, true)
	if err != nil {
		t.Fatal(err)
	}
	if len(products) != 2 || len(runs) != 1 {
		t.Fatalf("%d product parts, %d runs parts", len(products), len(runs))
	}
	parts := db.PublishParts(t, head, products, runs)
	// The daily group is stored sparse, the census/pulse group whole.
	for cohort, points := range map[string]int{"daily-us": 1, "7a5d84127c244707dc0e": 288} {
		n := count(t, db, fmt.Sprintf("SELECT jsonb_array_length(part->'windows'->'24h'->'series') FROM calculated_snapshot_parts WHERE build_id=%d AND cohort='%s'", parts, cohort))
		if n != points {
			t.Errorf("%s: %d stored 24h points, want %d", cohort, n, points)
		}
	}
	in = load(t, db)
	if in.Snapshot.BuildID != parts {
		t.Fatalf("read build %d, want %d", in.Snapshot.BuildID, parts)
	}
	want := text(t, db, "SELECT (payload - 'runs')::text FROM calculated_snapshots WHERE build_id=$1", whole)
	if got := string(in.Snapshot.Payload); got != want {
		at := 0
		for at < min(len(got), len(want)) && got[at] == want[at] {
			at++
		}
		t.Fatalf("parts read differently from byte %d:\n got %.300s\nwant %.300s", at, got[at:], want[at:])
	}
}

// The head's product part count must match the stored parts.
func TestPartsMustMatchTheirHead(t *testing.T) {
	db := testdb.New(t)
	payload, err := testscale.EveryRequest()
	if err != nil {
		t.Fatal(err)
	}
	head, products, runs, err := testscale.Parts(payload, true)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct{ name, from, to, want string }{
		{"one part missing", `"products":2`, `"products":3`, "its head counts 3 product parts, 2 are stored"},
		{"no count", `"products":2,`, ``, "no product part count"},
	} {
		changed := strings.Replace(string(head), c.from, c.to, 1)
		if changed == string(head) {
			t.Fatalf("%s: %s not in the head", c.name, c.from)
		}
		db.PublishParts(t, []byte(changed), products, runs)
		if _, err := tryLoad(t, db); err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: %v, want %q", c.name, err, c.want)
		}
	}
	// A stored point off its window's grid fails the read rather than vanish.
	var daily map[string]any
	if err := json.Unmarshal(products[1], &daily); err != nil {
		t.Fatal(err)
	}
	kept := daily["windows"].(map[string]any)["24h"].(map[string]any)["series"].([]any)[0].(map[string]any)
	kept["at"] = strings.Replace(kept["at"].(string), ":00+00:00", ":01+00:00", 1)
	off, err := json.Marshal(daily)
	if err != nil {
		t.Fatal(err)
	}
	db.PublishParts(t, head, [][]byte{products[0], off}, runs)
	if _, err := tryLoad(t, db); err == nil || !strings.Contains(err.Error(), "product part 1: window 24h: 1 series points off the 5m0s grid") {
		t.Errorf("off the grid: %v", err)
	}
}
