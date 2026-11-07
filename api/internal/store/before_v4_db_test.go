package store_test

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
	"github.com/jackc/pgx/v5"
)

// statements records the SQL a connection runs.
type statements struct {
	mu  sync.Mutex
	sql []string
}

func (s *statements) TraceQueryStart(ctx context.Context, _ *pgx.Conn, d pgx.TraceQueryStartData) context.Context {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sql = append(s.sql, d.SQL)
	return ctx
}

func (s *statements) TraceQueryEnd(context.Context, *pgx.Conn, pgx.TraceQueryEndData) {}

// headAndGroups is the every-request fixture as a head of schema_version
// version, stored in parts, and its product groups.
func headAndGroups(t *testing.T, version int) ([]byte, [][]byte) {
	t.Helper()
	raw, err := testscale.EveryRequest()
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]json.RawMessage
	var groups []json.RawMessage
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(doc["products"], &groups); err != nil {
		t.Fatal(err)
	}
	delete(doc, "products")
	doc["schema_version"], _ = json.Marshal(version)
	doc["parts"], _ = json.Marshal(map[string]int{"products": len(groups), "runs": 0})
	head, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	parts := make([][]byte, len(groups))
	for i, g := range groups {
		parts[i] = g
	}
	return head, parts
}

// A build before payload v4 is ErrBeforeV4: a v3 head stored in parts, a
// head of schema_version 4 without networks and a whole v2 payload. The load
// reads neither its product parts nor a list: no statement names
// calculated_snapshot_parts, calculated_windows or a list table.
func TestABuildBeforeV4IsErrBeforeV4(t *testing.T) {
	t.Parallel()
	for name, publish := range map[string]func(*testing.T, *testdb.DB) int64{
		"v3 head": func(t *testing.T, db *testdb.DB) int64 {
			head, groups := headAndGroups(t, 3)
			return db.PublishParts(t, head, groups, nil)
		},
		"v4 head without networks": func(t *testing.T, db *testdb.DB) int64 {
			head, groups := headAndGroups(t, 4)
			return db.PublishParts(t, head, groups, nil)
		},
		"v2 payload": func(t *testing.T, db *testdb.DB) int64 {
			raw, err := testscale.EveryRequest()
			if err != nil {
				t.Fatal(err)
			}
			return db.Publish(t, raw)
		},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			db := testdb.New(t)
			publish(t, db)
			ctx := context.Background()
			cfg, err := pgx.ParseConfig(db.DSN)
			if err != nil {
				t.Fatal(err)
			}
			log := &statements{}
			cfg.Tracer = log
			conn, err := pgx.ConnectConfig(ctx, cfg)
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close(ctx)
			in, err := store.LoadLists(ctx, conn, store.NewLists())
			if !errors.Is(err, store.ErrBeforeV4) || in != nil {
				t.Fatalf("load: %v (inputs %v), want ErrBeforeV4", err, in != nil)
			}
			for _, table := range []string{"calculated_snapshot_parts", "calculated_windows", "calculated_list_rows", "calculated_breakdowns"} {
				if i := slices.IndexFunc(log.sql, func(q string) bool { return strings.Contains(q, table) }); i >= 0 {
					t.Errorf("the load read %s: %s", table, log.sql[i])
				}
			}
		})
	}
}
