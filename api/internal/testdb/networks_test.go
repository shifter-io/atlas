package testdb_test

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"slices"
	"sort"
	"sync"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/config"
	"github.com/Zeus-Development/proxy-index/api/internal/server"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
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

// ran is the names of the registry's statements of a layout (store.Statement
// Only: "v4" or "pre-v4") the log holds.
func ran(log *statements, only string) []string {
	var out []string
	for _, s := range store.Statements {
		if sql, _ := s.Query(&store.ExplainParams{}); s.Only == only && slices.Contains(log.sql, sql) {
			out = append(out, s.Name)
		}
	}
	return out
}

// PublishV4 of each saved fixture loads through store.LoadLists as a v4
// build, reading its network lists and no product part or group list, and
// gives every answer the build of the fixture's lists from one source gives
// (testscale.Listed), but for the documented differences
// (testscale.AsV4Answers).
func TestPublishV4AnswersAsTheListedFixture(t *testing.T) {
	for _, name := range []string{"snapshot.json", "snapshot-every-request.json"} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			payload, err := os.ReadFile(config.FindUp("fixtures/measured/" + name))
			if err != nil {
				t.Fatal(err)
			}
			listed, err := testscale.Listed(payload)
			if err != nil {
				t.Fatal(err)
			}
			db := testdb.New(t)
			db.Publish(t, listed)
			before, _, log := answers(t, db)
			if len(ran(log, "pre-v4")) == 0 {
				t.Errorf("the v1/v2 read ran no pre-v4 statement: %q", log.sql)
			}
			want, err := testscale.AsV4Answers(before, groups(t, payload))
			if err != nil {
				t.Fatal(err)
			}
			id := db.PublishV4(t, payload)
			got, in, log := answers(t, db)
			if in.Snapshot == nil || in.Snapshot.BuildID != id || len(in.Snapshot.Networks) != 3 {
				t.Fatalf("read %+v, want build %d with 3 network lists", in.Snapshot, id)
			}
			if pre, v4 := ran(log, "pre-v4"), ran(log, "v4"); len(pre) > 0 || !slices.Contains(v4, "load.network_counts") {
				t.Errorf("the v4 read ran %v (want none) and %v (want load.network_counts)", pre, v4)
			}
			compare(t, want, got)
		})
	}
}

// groups is a payload's product groups.
func groups(t *testing.T, payload []byte) [][]byte {
	t.Helper()
	var s struct {
		Products []json.RawMessage `json:"products"`
	}
	if err := json.Unmarshal(payload, &s); err != nil {
		t.Fatal(err)
	}
	out := make([][]byte, len(s.Products))
	for i, g := range s.Products {
		out[i] = g
	}
	return out
}

// compare reports each answer that differs, around its first difference.
func compare(t *testing.T, want, got map[string][]byte) {
	t.Helper()
	keys := make([]string, 0, len(want))
	for k := range want {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		w, g := want[k], got[k]
		if bytes.Equal(w, g) {
			continue
		}
		i := 0
		for i < min(len(w), len(g)) && w[i] == g[i] {
			i++
		}
		from := max(i-120, 0)
		t.Errorf("%s differs at byte %d:\nwant …%s\ngot  …%s", k, i, w[from:min(len(w), i+200)], g[from:min(len(g), i+200)])
	}
	if len(got) != len(want) {
		t.Errorf("%d answers, want %d", len(got), len(want))
	}
}
