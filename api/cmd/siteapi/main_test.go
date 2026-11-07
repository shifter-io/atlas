package main

import (
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

func TestFlagUsagesNameRepositorySecrets(t *testing.T) {
	fs := flag.NewFlagSet("test", flag.ContinueOnError)
	var c common
	c.register(fs)
	f := fs.Lookup("database-file")
	if f == nil {
		t.Fatal("flag database-file is not registered")
	}
	if !strings.Contains(f.Usage, ".secrets/database.toml in the repository root") {
		t.Errorf("flag database-file usage %q: want .secrets/database.toml in the repository root", f.Usage)
	}
}

func TestWriteExplainWritesBothFiles(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "out")
	report := &store.ExplainReport{Statements: []store.Explained{{Name: "poll.site_layout", ExecutionMs: 1}}}
	if err := writeExplain(dir, report); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"explain.json", "explain.md"} {
		body, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil || !strings.Contains(string(body), "poll.site_layout") {
			t.Errorf("%s: %v\n%s", name, err, body)
		}
	}
}

func TestExplainNeedsOutAndADatabase(t *testing.T) {
	if err := explain(nil); err == nil || !strings.Contains(err.Error(), "--out is required") {
		t.Errorf("explain without --out: %v", err)
	}
	t.Setenv("DATABASE_URL", "postgres://nobody@127.0.0.1:1/none?connect_timeout=1&sslmode=disable")
	if err := explain([]string{"--out", t.TempDir()}); err == nil {
		t.Error("explain without a database: no error")
	}
}
