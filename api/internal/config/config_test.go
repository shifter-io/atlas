package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// repoTree makes a repository root in a temporary folder: one holding the
// framework tree.
func repoTree(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, FrameworkTree), 0o755); err != nil {
		t.Fatal(err)
	}
	return root
}

func sameFile(t *testing.T, got, want string) {
	t.Helper()
	gotInfo, err := os.Stat(got)
	if err != nil {
		t.Fatalf("%q: %v", got, err)
	}
	wantInfo, err := os.Stat(want)
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(gotInfo, wantInfo) {
		t.Fatalf("got %q, want %q", got, want)
	}
}

func TestRepoRootEmptyWithoutFrameworkTree(t *testing.T) {
	root := t.TempDir()
	writeDatabaseTOML(t, filepath.Join(root, ".secrets", "database.toml"))
	t.Chdir(root)
	if got := RepoRoot(); got != "" {
		t.Fatalf("RepoRoot = %q, want empty", got)
	}
	if got := RepoPath(".secrets", "database.toml"); got != "" {
		t.Fatalf("RepoPath = %q, want empty", got)
	}
}

func TestRepoRootFindsFrameworkTreeAboveWorkingDirectory(t *testing.T) {
	root := repoTree(t)
	nested := filepath.Join(root, "api", "internal")
	if err := os.MkdirAll(nested, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Chdir(nested)
	sameFile(t, RepoRoot(), root)
	got := RepoPath(FrameworkTree)
	if !strings.HasSuffix(got, filepath.Join("src", "proxy_index")) {
		t.Fatalf("RepoPath = %q, want a path ending in src/proxy_index", got)
	}
	sameFile(t, got, filepath.Join(root, FrameworkTree))
	if got := RepoPath(".secrets", "database.toml"); got != "" {
		t.Fatalf("RepoPath of a missing file = %q, want empty", got)
	}
}

// The API lives in api/ of the framework's repository: from this package the
// root is three folders up and holds the framework's schema.
func TestRepoRootIsThisRepository(t *testing.T) {
	sameFile(t, RepoRoot(), filepath.Join("..", "..", ".."))
	if RepoPath(FrameworkTree, "schema.sql") == "" {
		t.Fatal("no src/proxy_index/schema.sql in the repository root")
	}
}

func TestResolveDSNPrecedence(t *testing.T) {
	root := repoTree(t)
	writeDatabaseTOML(t, filepath.Join(root, ".secrets", "database.toml"))
	explicit := filepath.Join(root, "explicit.toml")
	if err := os.WriteFile(explicit, []byte("[postgres]\nhost = \"127.0.0.1\"\ndbname = \"explicit\"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Chdir(root)

	if got, err := ResolveDSN("postgres://127.0.0.1/url", explicit); err != nil || got != "postgres://127.0.0.1/url" {
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

func TestFindUpReturnsThePathInTheNearestFolder(t *testing.T) {
	root := repoTree(t)
	t.Chdir(filepath.Join(root, "src"))
	sameFile(t, FindUp("proxy_index"), filepath.Join(root, FrameworkTree))
	if got := FindUp("missing"); got != "" {
		t.Fatalf("FindUp = %q, want empty", got)
	}
}
