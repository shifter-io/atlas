// Command siteapi serves every piece of data the website needs from the
// proxy_reviews database, precomputed and precompressed in memory, and the
// dataset as a file on disk.
//
//	siteapi serve        [--listen 127.0.0.1:8090] [--database-file F] [--interval 10s] [--files-dir D] [--mem-trace DIR] [--pprof 127.0.0.1:6060]
//	                     [--admin-listen 127.0.0.1:8190]
//	siteapi migrate      [--database-file F]
//	siteapi dump --out dataset.json [--endpoint /v1/dataset] [--pretty]
//	siteapi explain --out DIR [--database-file F]
//	siteapi bench --base-url URL [--admin-url URL] --out DIR [--warm 200] [--concurrency 8] [--budgets F] [--gaps F]
//
// The database comes from DATABASE_URL, --database-file, or
// .secrets/database.toml in the repository root (the working directory or its
// nearest parent holding src/atlas).
// serve and dump need only the framework's schema: site tables that are
// missing (before the first `siteapi migrate`) are served empty.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/bench"
	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/config"
	"github.com/Zeus-Development/atlas/api/internal/server"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

func usage() {
	fmt.Fprintln(os.Stderr, `usage: siteapi <command> [flags]

commands:
  serve        serve the cached JSON API (read-only database access)
  migrate      create or upgrade the "site" schema (catalog, pricing, trust, reviews)
  dump         write one response (default /v1/dataset) to a file, for static builds
  explain      time every API statement with EXPLAIN ANALYZE (read-only role)
  bench        time every page's requests against a running siteapi

run "siteapi <command> -h" for flags`)
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

type common struct {
	databaseFile string
}

func (c *common) register(fs *flag.FlagSet) {
	fs.StringVar(&c.databaseFile, "database-file", os.Getenv("SITEAPI_DATABASE_FILE"), "framework credentials TOML with a [postgres] section (default: .secrets/database.toml in the repository root); DATABASE_URL wins")
}

func (c *common) dsn() (string, error) {
	return config.ResolveDSN(os.Getenv("DATABASE_URL"), c.databaseFile)
}

func printJSON(v any) {
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	enc.Encode(v) //nolint:errcheck
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	log := slog.New(slog.NewTextHandler(os.Stderr, nil))
	slog.SetDefault(log)
	cmd, args := os.Args[1], os.Args[2:]
	var err error
	switch cmd {
	case "serve":
		err = serve(args)
	case "migrate":
		err = migrate(args)
	case "dump":
		err = dump(args)
	case "explain":
		err = explain(args)
	case "bench":
		err = runBench(args)
	case "-h", "--help", "help":
		usage()
		return
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "siteapi "+cmd+":", err)
		os.Exit(1)
	}
}

// serveFlags holds the serve command's flags.
type serveFlags struct {
	common
	listen, filesDir, memTrace, pprof string
	adminListen                       string
	interval, dataPeriod, cacheFloor  time.Duration
	rowCacheMiB                       int
	cdnTTL, cdnTag                    string
}

func parseServe(args []string) serveFlags {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	var f serveFlags
	f.register(fs)
	fs.StringVar(&f.listen, "listen", env("SITEAPI_LISTEN", "127.0.0.1:8090"), "listen address")
	fs.DurationVar(&f.interval, "interval", 10*time.Second, "change-detection interval")
	fs.DurationVar(&f.dataPeriod, "data-period", server.DefaultDataPeriod, "data update schedule: data answers are cached until the published snapshot's next period")
	fs.DurationVar(&f.cacheFloor, "cache-floor", server.DefaultCacheFloor, "data answer TTL when the next update is unknown or overdue")
	fs.StringVar(&f.filesDir, "files-dir", env("SITEAPI_FILES_DIR", "/var/lib/proxy-index-api/files"), "directory the dataset file is written to and served from")
	fs.IntVar(&f.rowCacheMiB, "row-cache", store.DefaultRowCacheBytes>>20, "MiB of network list rows kept in memory; a rebuild fills it before it serves, each list's smallest part first. "+
		"The default holds the two real days' 265 lists, 311 MB; /healthz coldLists names those left out")
	fs.StringVar(&f.memTrace, "mem-trace", "", "directory for a memory trace: the RSS every 200 ms (rss.tsv), each rebuild phase's start and peak in the log, the heap profile at the peak (heap.pprof)")
	fs.StringVar(&f.pprof, "pprof", "", "loopback address to serve Go's profiles on (net/http/pprof), for example 127.0.0.1:6060")
	fs.StringVar(&f.cdnTTL, "cdn-ttl", env("SITEAPI_CDN_TTL", "0"), "with a CDN in front: how long it keeps a data answer (browsers 60 s), such as 1h; 0 is off: data answers are cached until the next update")
	fs.StringVar(&f.cdnTag, "cdn-tag", env("SITEAPI_CDN_TAG", server.DefaultCDNTag), "with --cdn-ttl: the CDN-Tag data answers carry, which a purge names")
	fs.StringVar(&f.adminListen, "admin-listen", env("SITEAPI_ADMIN_LISTEN", ""),
		"loopback address serving the full /healthz report (the public /healthz has only status, version, builtAt, revision and reason), for example 127.0.0.1:8190")
	fs.Parse(args) //nolint:errcheck
	return f
}

// cdn is the serve flags' CDN policy (server.Options CDNTTL and CDNTag).
func (f serveFlags) cdn() (time.Duration, string, error) {
	ttl, err := time.ParseDuration(f.cdnTTL)
	if err != nil {
		return 0, "", fmt.Errorf("cdn-ttl: %w", err)
	}
	return ttl, f.cdnTag, server.ValidCDN(ttl, f.cdnTag)
}

func serve(args []string) error {
	f := parseServe(args)
	if err := server.ValidDataPeriod(f.dataPeriod); err != nil {
		return err
	}
	if f.rowCacheMiB <= 0 {
		return fmt.Errorf("row cache: %d MiB, want more than 0", f.rowCacheMiB)
	}
	cdnTTL, cdnTag, err := f.cdn()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(f.filesDir, 0o755); err != nil {
		return fmt.Errorf("files directory: %w", err)
	}
	dsn, err := f.dsn()
	if err != nil {
		return err
	}
	setMemoryLimit("/")
	if err := startPprof(f.pprof); err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	ctx, stopTrace, err := startMemoryTrace(ctx, f.memTrace)
	if err != nil {
		return err
	}
	defer stopTrace()
	pool, err := store.OpenReadOnly(ctx, dsn)
	if err != nil {
		return err
	}
	defer pool.Close()
	b := server.BinaryInfo()
	slog.Info("siteapi starting", "revision", deref(b.Revision, "unknown"), "revision_time", deref(b.Time, "unknown"), "modified", b.Modified != nil && *b.Modified, "cdn_ttl", cdnTTL)
	srv := server.New(server.DBSource{Pool: pool, Lists: &store.Lists{RowCacheBytes: f.rowCacheMiB << 20}}, server.Options{Interval: f.interval, DataPeriod: f.dataPeriod, CacheFloor: f.cacheFloor,
		FilesDir: f.filesDir, CDNTTL: cdnTTL, CDNTag: cdnTag})
	if _, err := startAdmin(f.adminListen, srv.AdminHandler()); err != nil {
		return err
	}
	// A build older than payload v4 serves nothing, but /healthz says why.
	if _, err := srv.Refresh(ctx, true); err != nil && !errors.Is(err, store.ErrBeforeV4) {
		return fmt.Errorf("initial build: %w", err)
	}
	return serveUntilDone(ctx, srv, f.listen)
}

// stopWait is how long a shutdown waits for a rebuild in progress to stop
// (seconds: it checks between its steps) before closing the database pool.
var stopWait = 5 * time.Second

// serveUntilDone serves srv on addr and refreshes it until ctx ends (SIGTERM),
// then returns once the HTTP server has shut down and the refresh loop has
// stopped: a rebuild in progress is discarded, never left reading through a
// pool being closed (deploy 12: the stop waited for one and was SIGKILLed).
func serveUntilDone(ctx context.Context, srv *server.Server, addr string) error {
	ran := make(chan struct{})
	go func() { srv.Run(ctx); close(ran) }()
	err := listenAndServe(ctx, addr, srv.Handler())
	select {
	case <-ran:
	case <-time.After(stopWait):
		slog.Warn("rebuild still running at exit", "waited", stopWait)
	}
	return err
}

// listenAndServe serves h on addr until ctx ends or the listener fails, then
// shuts the server down, waiting at most 10 seconds for open requests.
func listenAndServe(ctx context.Context, addr string, h http.Handler) error {
	hs := newHTTPServer(addr, h)
	errc := make(chan error, 1)
	go func() { errc <- hs.ListenAndServe() }()
	slog.Info("siteapi listening", "addr", addr)
	select {
	case <-ctx.Done():
	case err := <-errc:
		if !errors.Is(err, http.ErrServerClosed) {
			return err
		}
	}
	shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return hs.Shutdown(shutdown)
}

// newHTTPServer is the API's server on addr: every timeout set, so a slow
// client cannot hold a connection, and headers above 1 MB answer 431.
func newHTTPServer(addr string, h http.Handler) *http.Server {
	return &http.Server{Addr: addr, Handler: h, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second,
		WriteTimeout: 30 * time.Second, IdleTimeout: 120 * time.Second, MaxHeaderBytes: 1 << 20}
}

func migrate(args []string) error {
	fs := flag.NewFlagSet("migrate", flag.ExitOnError)
	var c common
	c.register(fs)
	fs.Parse(args) //nolint:errcheck
	dsn, err := c.dsn()
	if err != nil {
		return err
	}
	ctx := context.Background()
	conn, err := store.OpenWrite(ctx, dsn)
	if err != nil {
		return err
	}
	defer conn.Close(ctx)
	res, err := store.Migrate(ctx, conn)
	if err != nil {
		return err
	}
	printJSON(res)
	return nil
}

// dumpFlags holds the dump command's flags.
type dumpFlags struct {
	common
	out, endpoint string
	pretty        bool
}

func parseDump(args []string) dumpFlags {
	fs := flag.NewFlagSet("dump", flag.ExitOnError)
	var f dumpFlags
	f.register(fs)
	fs.StringVar(&f.out, "out", "", "output file (required)")
	fs.StringVar(&f.endpoint, "endpoint", "/v1/dataset", "which response to write: /v1/dataset, /v1/coverage, /v1/coverage/<slug>, /v1/networks, /v1/rankings/<range>, /v1/countries")
	fs.BoolVar(&f.pretty, "pretty", false, "indent the JSON")
	fs.Parse(args) //nolint:errcheck
	return f
}

func dump(args []string) error {
	f := parseDump(args)
	if f.out == "" {
		return errors.New("--out is required")
	}
	res, err := buildFromDatabase(f.common)
	if err != nil {
		return err
	}
	rendered, err := renderEndpoint(res, f.endpoint)
	if err != nil {
		return err
	}
	body := rendered.Identity
	if f.pretty {
		var buf bytes.Buffer
		if err := json.Indent(&buf, body, "", "  "); err == nil {
			body = buf.Bytes()
		}
	}
	if err := os.WriteFile(f.out, append(body, '\n'), 0o644); err != nil {
		return err
	}
	bySource := map[string]int{}
	for _, p := range res.Dataset.Providers {
		bySource[p.Source]++
	}
	printJSON(map[string]any{"out": f.out, "endpoint": f.endpoint, "bytes": len(body) + 1, "status": rendered.Status, "providers": bySource,
		"generatedAt": res.Dataset.GeneratedAt, "snapshotPublished": res.Dataset.Snapshot.Available,
		"naFields": res.Coverage.Summary.NAFields, "naFieldsBySource": res.Coverage.Summary.NAFieldsBySource})
	return nil
}

// buildFromDatabase loads the inputs from the database c names, read-only,
// and builds every answer from them.
func buildFromDatabase(c common) (*build.Result, error) {
	dsn, err := c.dsn()
	if err != nil {
		return nil, err
	}
	ctx := context.Background()
	pool, err := store.OpenReadOnly(ctx, dsn)
	if err != nil {
		return nil, err
	}
	defer pool.Close()
	in, err := store.Load(ctx, pool)
	if err != nil {
		return nil, err
	}
	return build.Build(in)
}

// renderEndpoint is the response serve gives for endpoint, rendered from res.
func renderEndpoint(res *build.Result, endpoint string) (*server.Rendered, error) {
	v, err := server.RenderVersion(0, res, server.Options{})
	if err != nil {
		return nil, err
	}
	if endpoint == "/v1/dataset" {
		// The dataset is served as a file (/v1/dataset redirects to it),
		// written by the same streaming writer.
		var buf bytes.Buffer
		if err := build.WriteDataset(&buf, res.Dataset); err != nil {
			return nil, err
		}
		return &server.Rendered{Status: http.StatusOK, Identity: buf.Bytes()}, nil
	}
	rendered, ok := v.Static[endpoint]
	if !ok {
		return nil, fmt.Errorf("unknown endpoint %s", endpoint)
	}
	return rendered, nil
}

// explain writes explain.json and explain.md to --out: every statement the
// API runs, timed with EXPLAIN ANALYZE through a read-only role.
func explain(args []string) error {
	fs := flag.NewFlagSet("explain", flag.ExitOnError)
	var c common
	c.register(fs)
	out := fs.String("out", "", "output directory (required): explain.json and explain.md are written there")
	fs.Parse(args) //nolint:errcheck
	if *out == "" {
		return errors.New("--out is required")
	}
	dsn, err := c.dsn()
	if err != nil {
		return err
	}
	ctx := context.Background()
	pool, err := store.OpenReadOnly(ctx, dsn)
	if err != nil {
		return err
	}
	defer pool.Close()
	report, err := store.Explain(ctx, pool)
	if err != nil {
		return err
	}
	return writeExplain(*out, report)
}

// writeExplain writes the report's two files to dir, creating it.
func writeExplain(dir string, report *store.ExplainReport) error {
	body, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(dir, "explain.json"), append(body, '\n'), 0o644); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(dir, "explain.md"), report.Markdown(), 0o644); err != nil {
		return err
	}
	printJSON(map[string]any{"out": dir, "statements": len(report.Statements), "build": report.Params.Build})
	return nil
}

// runBench writes bench.json and bench.md to --out: every page's requests
// timed against the siteapi at --base-url. It fails when a budget is
// exceeded or a request does not answer 200. With --gaps it then writes
// gaps.json and gaps.md there, the verdict of every map gap, and also fails
// when a gap fails.
func runBench(args []string) error {
	fs := flag.NewFlagSet("bench", flag.ExitOnError)
	base := fs.String("base-url", "", "the running siteapi, such as http://127.0.0.1:8090 (required)")
	admin := fs.String("admin-url", "", "its admin listener (serve --admin-listen), such as http://127.0.0.1:8190: the build timings come from its /healthz")
	out := fs.String("out", "", "output directory (required): bench.json and bench.md are written there")
	warm := fs.Int("warm", 200, "warm requests per request, after the cold one")
	conc := fs.Int("concurrency", 8, "workers, each with its own keep-alive connection")
	budgets := fs.String("budgets", "", "JSON budgets file laid over the defaults (api/README.md, \"siteapi bench\")")
	gaps := fs.String("gaps", "", "gap manifest (docs/api-web-map-gaps.json): check every API gap and write gaps.json and gaps.md")
	fs.Parse(args) //nolint:errcheck
	if *base == "" || *out == "" {
		return errors.New("--base-url and --out are required")
	}
	b, err := bench.LoadBudgets(*budgets)
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	report, err := bench.Run(ctx, bench.Options{BaseURL: *base, AdminURL: *admin, Warm: *warm, Concurrency: *conc, Budgets: b})
	if err != nil {
		return err
	}
	if err := report.Write(*out); err != nil {
		return err
	}
	// After the bench: the gap checks request the same URLs, so before it
	// they would warm the list pages the bench times cold.
	gapErr, err := checkGaps(ctx, *gaps, *base, *out)
	if err != nil {
		return err
	}
	printJSON(map[string]any{"out": *out, "pages": len(report.Pages), "requests": len(report.Requests), "failures": len(report.Failures)})
	if report.Failed() {
		return errors.Join(fmt.Errorf("%d over budget or failed; see %s", len(report.Failures), filepath.Join(*out, "bench.md")), gapErr)
	}
	return gapErr
}

// checkGaps writes gaps.json and gaps.md to out for the manifest (none when
// manifest is ""). failed is not nil when a gap fails.
func checkGaps(ctx context.Context, manifest, base, out string) (failed, err error) {
	if manifest == "" {
		return nil, nil
	}
	m, err := bench.LoadManifest(manifest)
	if err != nil {
		return nil, err
	}
	r, err := bench.CheckGaps(ctx, base, m)
	if err != nil {
		return nil, err
	}
	if err := r.Write(out); err != nil {
		return nil, err
	}
	printJSON(map[string]any{"out": out, "gapsPassed": r.Passed, "gapsFailed": r.Failed, "gapsListed": r.Listed})
	if r.Failed > 0 {
		return fmt.Errorf("%d map gaps fail; see %s", r.Failed, filepath.Join(out, "gaps.md")), nil
	}
	return nil, nil
}

// deref is *p, or def when p is nil.
func deref(p *string, def string) string {
	if p == nil {
		return def
	}
	return *p
}
