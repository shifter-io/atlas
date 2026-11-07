package testdb_test

import (
	"context"
	"os"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/config"
	"github.com/Zeus-Development/proxy-index/api/internal/server"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/jackc/pgx/v5"
)

// statements records every statement a connection runs.
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

// answers is every answer the server renders of the current build, and its
// pages, read through store.LoadLists over one logged connection.
func answers(t *testing.T, db *testdb.DB) (map[string][]byte, *store.Inputs, *statements) {
	t.Helper()
	db.Exec(t, "UPDATE calculated_snapshots SET content_hash = 'same'")
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
	if err != nil {
		t.Fatal(err)
	}
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	res.BuiltAt = time.Date(2026, 11, 7, 0, 0, 0, 0, time.UTC)
	res.Coverage.BuiltAt = build.JSISO(res.BuiltAt)
	v, err := server.RenderVersion(1, res, server.Options{FilesDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	out := map[string][]byte{}
	for p, r := range v.Static {
		if p != "/v1/version" {
			out[p] = r.Identity
		}
	}
	if out["pages"], err = server.Marshal(res.Pages); err != nil {
		t.Fatal(err)
	}
	return out, in, log
}

// ran is the names of the registry's v4 statements (store.Statement Only)
// the log holds.
func ran(log *statements) []string {
	var out []string
	for _, s := range store.Statements {
		if sql, _ := s.Query(&store.ExplainParams{}); s.Only == "v4" && slices.Contains(log.sql, sql) {
			out = append(out, s.Name)
		}
	}
	return out
}

// PublishV4 of each saved fixture loads through store.LoadLists as a v4
// build, reading its network lists and no product part or group list, and
// renders every answer. Until payload v4 was the only one read, this test
// also showed that it gives every answer the build of the fixture's lists
// from one source gives (testscale.Listed), but for the documented
// differences (testscale.AsV4Answers).
func TestPublishV4LoadsAsAV4Build(t *testing.T) {
	for _, name := range []string{"snapshot.json", "snapshot-every-request.json"} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			payload, err := os.ReadFile(config.FindUp("fixtures/measured/" + name))
			if err != nil {
				t.Fatal(err)
			}
			db := testdb.New(t)
			id := db.PublishV4(t, payload)
			got, in, log := answers(t, db)
			if in.Snapshot == nil || in.Snapshot.BuildID != id || len(in.Snapshot.Networks) != 3 {
				t.Fatalf("read %+v, want build %d with 3 network lists", in.Snapshot, id)
			}
			if !slices.Contains(ran(log), "load.network_counts") || len(got) == 0 {
				t.Errorf("the v4 read ran %v (want load.network_counts) and rendered %d answers", ran(log), len(got))
			}
			for _, table := range []string{"calculated_snapshot_parts", "calculated_windows", "calculated_breakdowns"} {
				if i := slices.IndexFunc(log.sql, func(q string) bool { return strings.Contains(q, table) }); i >= 0 {
					t.Errorf("the v4 read read %s: %s", table, log.sql[i])
				}
			}
		})
	}
}
