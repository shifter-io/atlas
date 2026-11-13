package main

import (
	"flag"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
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
	// The build timings come from the admin listener's /healthz.
	admin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"status":"ok","version":1,"builtAt":"2026-11-08T00:00:00Z","buildMs":4242,"loadMs":11}`)
	}))
	defer admin.Close()
	_ = runBench([]string{"--base-url", ts.URL, "--admin-url", admin.URL + "/", "--out", dir, "--warm", "1", "--concurrency", "1"})
	if md, err := os.ReadFile(filepath.Join(dir, "bench.md")); err != nil || !strings.Contains(string(md), "| ok | 4242 ms | 11 ms |") {
		t.Errorf("bench.md with --admin-url: %v\n%s", err, md)
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

// The gap checks request the URLs the bench times, so they must come after
// every timed request, or the bench's cold requests would find warm pages.
func TestBenchGapsRunAfterTheTimedRequests(t *testing.T) {
	var mu sync.Mutex
	var order []string
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		order = append(order, r.Header.Get("Accept-Encoding")+" "+r.URL.RequestURI())
		mu.Unlock()
		switch r.URL.Path {
		case "/healthz":
			fmt.Fprint(w, `{"status":"ok","version":1,"builtAt":"2026-11-08T00:00:00Z"}`)
		case "/v1/version":
			fmt.Fprint(w, `{"revision":"abc"}`)
		case "/v1/networks":
			fmt.Fprint(w, `{"networks":[{"slug":"a","kind":"residential","kinds":["residential"]}]}`)
		default:
			fmt.Fprint(w, `{"countries":[],"x":1}`)
		}
	}))
	defer ts.Close()
	dir := t.TempDir()
	gaps := filepath.Join(dir, "m.json")
	if err := os.WriteFile(gaps, []byte(`{"gaps":[{"id":"G","class":"api","checks":[{"request":"/v1/rankings?range={range}","paths":["x"]}]}]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	budgets := filepath.Join(dir, "b.json")
	if err := os.WriteFile(budgets, []byte(`{"classes":{"answer":{},"events":{},"search":{},"list":{},"list-search":{}},"pageMs":0}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := runBench([]string{"--base-url", ts.URL, "--out", dir, "--warm", "1", "--concurrency", "1", "--budgets", budgets, "--gaps", gaps}); err != nil {
		t.Fatal(err)
	}
	firstGap, lastTimed := -1, -1
	for i, r := range order {
		if strings.HasPrefix(r, "br ") || strings.HasPrefix(r, "identity ") {
			lastTimed = i
		} else if firstGap < 0 && strings.HasPrefix(r, " /v1/rankings") {
			firstGap = i
		}
	}
	if firstGap < 0 || lastTimed < 0 || firstGap < lastTimed {
		t.Errorf("first gap request %d, last timed request %d: %v", firstGap, lastTimed, order)
	}
}
