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

func TestResolveDSNFindsSecretsInRepositoryRoot(t *testing.T) {
	root := repoTree(t)
	writeDatabaseTOML(t, filepath.Join(root, ".secrets", "database.toml"))
	nested := filepath.Join(root, "api", "cmd")
	if err := os.MkdirAll(nested, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Chdir(nested)
	dsn, err := ResolveDSN("", "")
	if err != nil {
		t.Fatalf("ResolveDSN: %v", err)
	}
	if !strings.Contains(dsn, "dbname='d'") {
		t.Fatalf("DSN %q does not come from .secrets/database.toml in the repository root", dsn)
	}
}

// A .secrets folder nearer than the repository root is not the repository's.
func TestResolveDSNIgnoresSecretsOutsideRepositoryRoot(t *testing.T) {
	root := repoTree(t)
	writeDatabaseTOML(t, filepath.Join(root, "api", ".secrets", "database.toml"))
	t.Chdir(filepath.Join(root, "api"))
	if dsn, err := ResolveDSN("", ""); err == nil {
		t.Fatalf("ResolveDSN read api/.secrets/database.toml: %q", dsn)
	}
}

func TestResolveDSNErrorNamesSecretsFile(t *testing.T) {
	t.Chdir(t.TempDir())
	dsn, err := ResolveDSN("", "")
	if err == nil {
		t.Fatalf("ResolveDSN found a database file in an empty folder: %q", dsn)
	}
	if !strings.Contains(err.Error(), ".secrets/database.toml") {
		t.Fatalf("error %q does not name .secrets/database.toml", err)
	}
}
