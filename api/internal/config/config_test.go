package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestFrameworkPathEmptyWhenAbsent(t *testing.T) {
	t.Chdir(t.TempDir())
	if got := FrameworkPath(".secrets", "database.toml"); got != "" {
		t.Fatalf("FrameworkPath = %q, want empty", got)
	}
}

func TestFrameworkPathFindsFolderAboveWorkingDirectory(t *testing.T) {
	root := t.TempDir()
	want := filepath.Join(root, FrameworkFolder, "src", "proxy_index")
	if err := os.MkdirAll(want, 0o755); err != nil {
		t.Fatal(err)
	}
	nested := filepath.Join(root, "api", "internal")
	if err := os.MkdirAll(nested, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Chdir(nested)
	got := FrameworkPath("src", "proxy_index")
	if !strings.HasSuffix(got, filepath.Join("proxy-index", "src", "proxy_index")) {
		t.Fatalf("FrameworkPath = %q, want a path ending in proxy-index/src/proxy_index", got)
	}
	gotInfo, err := os.Stat(got)
	if err != nil {
		t.Fatal(err)
	}
	wantInfo, err := os.Stat(want)
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(gotInfo, wantInfo) {
		t.Fatalf("FrameworkPath = %q, want %q", got, want)
	}
}

func TestResolveDSNPrecedence(t *testing.T) {
	root := t.TempDir()
	writeDatabaseTOML(t, filepath.Join(root, FrameworkFolder, ".secrets", "database.toml"))
	explicit := filepath.Join(root, "explicit.toml")
	if err := os.WriteFile(explicit, []byte("[postgres]\nhost = \"127.0.0.1\"\ndbname = \"explicit\"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Chdir(root)

	if got, err := ResolveDSN("postgres://url", explicit); err != nil || got != "postgres://url" {
		t.Fatalf("DATABASE_URL must win: %q %v", got, err)
	}
	if got, err := ResolveDSN("", explicit); err != nil || !strings.Contains(got, "dbname='explicit'") {
		t.Fatalf("explicit file must beat the found one: %q %v", got, err)
	}
	if got, err := ResolveDSN("", ""); err != nil || !strings.Contains(got, "dbname='d'") {
		t.Fatalf("found file: %q %v", got, err)
	}
	if _, err := ResolveDSN("", filepath.Join(root, "missing.toml")); err == nil {
		t.Fatal("a missing explicit file must fail, not fall back")
	}
}
