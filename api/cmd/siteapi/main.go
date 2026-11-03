// Command siteapi serves every piece of data the website needs from the
// proxy_reviews database, precomputed and precompressed in memory.
//
//	siteapi serve        [--listen 127.0.0.1:8090] [--database-file F] [--interval 10s] [--content-dir D]
//	siteapi migrate      [--database-file F]
//	siteapi seed-demo    [--file fixtures/demo/design-demo.json] [--force] [--no-backup] [--backup-dir D]
//	siteapi unseed-demo  [--no-backup]
//	siteapi dump --out dataset.json [--endpoint /v1/dataset] [--pretty]
//
// The database comes from DATABASE_URL, --database-file, or
// proxy-index/.secrets/database.toml found above the working directory.
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

	"github.com/Zeus-Development/proxy-index-website/api/internal/build"
	"github.com/Zeus-Development/proxy-index-website/api/internal/config"
	"github.com/Zeus-Development/proxy-index-website/api/internal/server"
	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
)

func usage() {
	fmt.Fprintln(os.Stderr, `usage: siteapi <command> [flags]

commands:
  serve        serve the cached JSON API (read-only database access)
  migrate      create or upgrade the "site" schema
  seed-demo    load the four design fixtures as is_demo rows (pg_dump backup first)
  unseed-demo  delete every is_demo row (pg_dump backup first)
  dump         write one response (default /v1/dataset) to a file, for static builds

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
	fs.StringVar(&c.databaseFile, "database-file", os.Getenv("SITEAPI_DATABASE_FILE"), "framework credentials TOML with a [postgres] section (default: proxy-index/.secrets/database.toml above the working directory); DATABASE_URL wins")
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
	case "seed-demo":
		err = seedDemo(args)
	case "unseed-demo":
		err = unseedDemo(args)
	case "dump":
		err = dump(args)
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

func contentDir(flagValue string) string {
	if flagValue == "-" {
		return ""
	}
	if flagValue != "" {
		return flagValue
	}
	return config.FindUp(filepath.Join("src", "content"))
}

func serve(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	var c common
	c.register(fs)
	listen := fs.String("listen", env("SITEAPI_LISTEN", "127.0.0.1:8090"), "listen address")
	interval := fs.Duration("interval", 10*time.Second, "change-detection interval")
	content := fs.String("content-dir", os.Getenv("SITEAPI_CONTENT_DIR"), "src/content directory for guide/review search entries (default: found above the working directory; - disables)")
	fs.Parse(args) //nolint:errcheck
	dsn, err := c.dsn()
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	pool, err := store.OpenReadOnly(ctx, dsn)
	if err != nil {
		return err
	}
	defer pool.Close()
	cnt, err := build.ReadContent(contentDir(*content))
	if err != nil {
		return fmt.Errorf("content: %w", err)
	}
	b := server.BinaryInfo()
	slog.Info("siteapi starting", "revision", deref(b.Revision, "unknown"), "revision_time", deref(b.Time, "unknown"), "modified", b.Modified != nil && *b.Modified)
	srv := server.New(server.DBSource{Pool: pool}, server.Options{Interval: *interval, Content: cnt})
	if _, err := srv.Refresh(ctx, true); err != nil {
		return fmt.Errorf("initial build: %w", err)
	}
	go srv.Run(ctx)
	hs := &http.Server{Addr: *listen, Handler: srv.Handler(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second,
		WriteTimeout: 30 * time.Second, IdleTimeout: 120 * time.Second}
	errc := make(chan error, 1)
	go func() { errc <- hs.ListenAndServe() }()
	slog.Info("siteapi listening", "addr", *listen)
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

type backupFlags struct {
	noBackup    bool
	backupDir   string
	runtimeFile string
}

func (b *backupFlags) register(fs *flag.FlagSet) {
	fs.BoolVar(&b.noBackup, "no-backup", false, "skip the pg_dump taken before writing")
	fs.StringVar(&b.backupDir, "backup-dir", "", "backup folder (default: proxy-index/.state/postgres/backups above the working directory)")
	fs.StringVar(&b.runtimeFile, "runtime-file", "", "Postgres runtime.json with bin_dir (default: proxy-index/.state/postgres/runtime.json)")
}

func (b *backupFlags) run(dsn, label string) (*store.BackupResult, error) {
	if b.noBackup {
		return nil, nil
	}
	runtime := b.runtimeFile
	if runtime == "" {
		runtime = config.FrameworkPath(".state", "postgres", "runtime.json")
	}
	if runtime == "" {
		return nil, errors.New("cannot find proxy-index/.state/postgres/runtime.json for pg_dump; pass --runtime-file or --no-backup")
	}
	dir := b.backupDir
	if dir == "" {
		dir = filepath.Join(filepath.Dir(runtime), "backups")
	}
	bin, err := store.PgBinDir(runtime)
	if err != nil {
		return nil, err
	}
	res, err := store.Backup(dsn, bin, dir, label)
	if err != nil {
		return nil, fmt.Errorf("backup: %w", err)
	}
	slog.Info("database backed up", "path", res.Path, "bytes", res.Bytes)
	return res, nil
}

func seedDemo(args []string) error {
	fs := flag.NewFlagSet("seed-demo", flag.ExitOnError)
	var c common
	c.register(fs)
	var b backupFlags
	b.register(fs)
	file := fs.String("file", "", "fixture file (default: fixtures/demo/design-demo.json above the working directory)")
	force := fs.Bool("force", false, "reload even when the same file is already loaded")
	fs.Parse(args) //nolint:errcheck
	if *file == "" {
		*file = config.FindUp(filepath.Join("fixtures", "demo", "design-demo.json"))
		if *file == "" {
			return errors.New("cannot find fixtures/demo/design-demo.json; pass --file")
		}
	}
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
	if err := store.CheckSchema(ctx, conn); err != nil {
		return err
	}
	var backup *store.BackupResult
	if loaded, err := store.DemoLoaded(ctx, conn, *file); err != nil {
		return err
	} else if !loaded || *force {
		// Nothing is written when the same file is already loaded, so only a real load is backed up.
		if backup, err = b.run(dsn, "before_seed_demo"); err != nil {
			return err
		}
	}
	res, err := store.SeedDemo(ctx, conn, *file, *force)
	if err != nil {
		return err
	}
	res.Backup = backup
	printJSON(res)
	return nil
}

func unseedDemo(args []string) error {
	fs := flag.NewFlagSet("unseed-demo", flag.ExitOnError)
	var c common
	c.register(fs)
	var b backupFlags
	b.register(fs)
	fs.Parse(args) //nolint:errcheck
	dsn, err := c.dsn()
	if err != nil {
		return err
	}
	backup, err := b.run(dsn, "before_unseed_demo")
	if err != nil {
		return err
	}
	ctx := context.Background()
	conn, err := store.OpenWrite(ctx, dsn)
	if err != nil {
		return err
	}
	defer conn.Close(ctx)
	deleted, err := store.UnseedDemo(ctx, conn)
	if err != nil {
		return err
	}
	printJSON(map[string]any{"deleted": deleted, "backup": backup})
	return nil
}

func dump(args []string) error {
	fs := flag.NewFlagSet("dump", flag.ExitOnError)
	var c common
	c.register(fs)
	out := fs.String("out", "", "output file (required)")
	endpoint := fs.String("endpoint", "/v1/dataset", "which response to write: /v1/dataset, /v1/search-index, /v1/coverage, /v1/coverage/<slug>, /v1/snapshot, /v1/networks, /v1/rankings/<range>, /v1/countries")
	pretty := fs.Bool("pretty", false, "indent the JSON")
	content := fs.String("content-dir", os.Getenv("SITEAPI_CONTENT_DIR"), "src/content directory for guide/review search entries (default: found above the working directory; - disables)")
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
	in, err := store.Load(ctx, pool)
	if err != nil {
		return err
	}
	cnt, err := build.ReadContent(contentDir(*content))
	if err != nil {
		return fmt.Errorf("content: %w", err)
	}
	res, err := build.Build(in, build.Options{Content: cnt})
	if err != nil {
		return err
	}
	v, err := server.RenderVersion(0, res, server.Options{})
	if err != nil {
		return err
	}
	rendered, ok := v.Static[*endpoint]
	if *endpoint == "/v1/dataset" {
		// The dataset is served as a file (/v1/dataset redirects to it).
		rendered, ok = v.Dataset, true
	}
	if !ok {
		return fmt.Errorf("unknown endpoint %s", *endpoint)
	}
	body := rendered.Identity
	if *pretty {
		var buf bytes.Buffer
		if err := json.Indent(&buf, body, "", "  "); err == nil {
			body = buf.Bytes()
		}
	}
	if err := os.WriteFile(*out, append(body, '\n'), 0o644); err != nil {
		return err
	}
	bySource := map[string]int{}
	for _, p := range res.Dataset.Providers {
		bySource[p.Source]++
	}
	printJSON(map[string]any{"out": *out, "endpoint": *endpoint, "bytes": len(body) + 1, "status": rendered.Status, "providers": bySource,
		"generatedAt": res.Dataset.GeneratedAt, "snapshotPublished": res.Dataset.Snapshot.Available,
		"naFields": res.Coverage.Summary.NAFields, "naFieldsBySource": res.Coverage.Summary.NAFieldsBySource})
	return nil
}

// deref is *p, or def when p is nil.
func deref(p *string, def string) string {
	if p == nil {
		return def
	}
	return *p
}
