package main

import (
	"flag"
	"fmt"
	"net/http"
	"net/http/httptest"
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

func TestBenchFailsOnAServerError(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/healthz":
			fmt.Fprint(w, `{"status":"ok","version":1,"builtAt":"2026-11-08T00:00:00Z"}`)
		case "/v1/version":
			fmt.Fprint(w, `{"revision":"abc"}`)
		case "/v1/networks":
			fmt.Fprint(w, `{"networks":[{"slug":"a","kind":"residential","kinds":["residential"]}]}`)
		case "/v1/countries":
			fmt.Fprint(w, `{"countries":[]}`)
		default:
			http.Error(w, "boom", http.StatusInternalServerError)
		}
	}))
	defer ts.Close()
	if err := runBench([]string{"--base-url", ts.URL}); err == nil || !strings.Contains(err.Error(), "--out are required") {
		t.Errorf("bench without --out: %v", err)
	}
	dir := filepath.Join(t.TempDir(), "out")
	err := runBench([]string{"--base-url", ts.URL, "--out", dir, "--warm", "2", "--concurrency", "2"})
	if err == nil || !strings.Contains(err.Error(), "over budget or failed") {
		t.Fatalf("bench against 500s: %v", err)
	}
	if md, err := os.ReadFile(filepath.Join(dir, "bench.md")); err != nil || !strings.Contains(string(md), "answers 500") {
		t.Errorf("bench.md: %v\n%s", err, md)
	}
	if err := runBench([]string{"--base-url", ts.URL, "--out", dir, "--budgets", filepath.Join(dir, "none.json")}); err == nil {
		t.Error("a missing budgets file: no error")
	}
	gaps := filepath.Join(dir, "gaps-manifest.json")
	if err := os.WriteFile(gaps, []byte(`{"gaps":[{"id":"G","page":1,"class":"api","work":"W6","issue":712,"checks":[{"request":"/v1/home","paths":["market"]}]}]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	err = runBench([]string{"--base-url", ts.URL, "--out", dir, "--warm", "1", "--concurrency", "1", "--gaps", gaps})
	if err == nil || !strings.Contains(err.Error(), "1 map gaps fail") || !strings.Contains(err.Error(), "over budget or failed") {
		t.Fatalf("bench --gaps against 500s: %v", err)
	}
	if md, err := os.ReadFile(filepath.Join(dir, "gaps.md")); err != nil || !strings.Contains(string(md), "answers 500") {
		t.Errorf("gaps.md: %v\n%s", err, md)
	}
	if err := runBench([]string{"--base-url", ts.URL, "--out", dir, "--gaps", filepath.Join(dir, "none.json")}); err == nil {
		t.Error("a missing gap manifest: no error")
	}
}
