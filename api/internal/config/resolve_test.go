package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const dummyDatabaseTOML = "[postgres]\nhost = \"127.0.0.1\"\nport = 1\ndbname = \"d\"\nuser = \"u\"\n"

func writeDatabaseTOML(t *testing.T, path string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(dummyDatabaseTOML), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestResolveDSNFindsProxyIndexFolder(t *testing.T) {
	root := t.TempDir()
	writeDatabaseTOML(t, filepath.Join(root, "proxy-index", ".secrets", "database.toml"))
	nested := filepath.Join(root, "a", "b")
	if err := os.MkdirAll(nested, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Chdir(nested)
	dsn, err := ResolveDSN("", "")
	if err != nil {
		t.Fatalf("ResolveDSN: %v", err)
	}
	if !strings.Contains(dsn, "dbname='d'") {
		t.Fatalf("DSN %q does not come from proxy-index/.secrets/database.toml", dsn)
	}
}

func TestResolveDSNReadsProxyIndexFolderInWorkingDirectory(t *testing.T) {
	root := t.TempDir()
	writeDatabaseTOML(t, filepath.Join(root, "proxy-index", ".secrets", "database.toml"))
	t.Chdir(root)
	dsn, err := ResolveDSN("", "")
	if err != nil {
		t.Fatalf("ResolveDSN did not read proxy-index/.secrets/database.toml: %v", err)
	}
	if !strings.Contains(dsn, "dbname='d'") {
		t.Fatalf("DSN %q does not come from proxy-index/.secrets/database.toml", dsn)
	}
}

func TestResolveDSNErrorNamesProxyIndexFolder(t *testing.T) {
	t.Chdir(t.TempDir())
	dsn, err := ResolveDSN("", "")
	if err == nil {
		t.Fatalf("ResolveDSN found a database file in an empty folder: %q", dsn)
	}
	if !strings.Contains(err.Error(), "proxy-index/.secrets/database.toml") {
		t.Fatalf("error %q does not name proxy-index/.secrets/database.toml", err)
	}
}
