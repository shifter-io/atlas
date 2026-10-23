package main

import (
	"encoding/json"
	"errors"
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestFlagUsagesNameProxyIndexFolder(t *testing.T) {
	fs := flag.NewFlagSet("test", flag.ContinueOnError)
	var c common
	c.register(fs)
	var b backupFlags
	b.register(fs)
	want := map[string]string{
		"database-file": "proxy-index/.secrets/database.toml",
		"backup-dir":    "proxy-index/.state/postgres/backups",
		"runtime-file":  "proxy-index/.state/postgres/runtime.json",
	}
	for name, path := range want {
		f := fs.Lookup(name)
		if f == nil {
			t.Fatalf("flag %s is not registered", name)
		}
		if !strings.Contains(f.Usage, path) {
			t.Errorf("flag %s usage %q: want %s", name, f.Usage, path)
		}
	}
}

func TestBackupWithoutRuntimeFileNamesProxyIndex(t *testing.T) {
	t.Chdir(t.TempDir())
	var b backupFlags
	_, err := b.run("dsn", "label")
	want := "cannot find proxy-index/.state/postgres/runtime.json for pg_dump; pass --runtime-file or --no-backup"
	if err == nil || err.Error() != want {
		t.Fatalf("run error = %v, want %q", err, want)
	}
}

func TestBackupReadsRuntimeFileUnderProxyIndex(t *testing.T) {
	root := t.TempDir()
	runtime := filepath.Join(root, "proxy-index", ".state", "postgres", "runtime.json")
	if err := os.MkdirAll(filepath.Dir(runtime), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(runtime, []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Chdir(root)
	var b backupFlags
	_, err := b.run("dsn", "label")
	var syntax *json.SyntaxError
	if !errors.As(err, &syntax) {
		t.Fatalf("run error = %v, want the runtime.json parse error", err)
	}
	if !strings.Contains(err.Error(), filepath.Join("proxy-index", ".state", "postgres", "runtime.json")) {
		t.Fatalf("run error %q does not name the found runtime.json", err)
	}
}
