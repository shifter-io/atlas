package testdb

import (
	"os"
	"path/filepath"
	"testing"
)

// The database tests load the framework schema from the repository's own
// framework tree, src/proxy_index three folders up, not from a pinned copy.
func TestFrameworkDirIsTheFrameworkTree(t *testing.T) {
	got := FrameworkDir()
	if got == "" {
		t.Fatal("FrameworkDir found no framework tree")
	}
	gotInfo, err := os.Stat(got)
	if err != nil {
		t.Fatal(err)
	}
	wantInfo, err := os.Stat(filepath.Join("..", "..", "..", "src", "proxy_index"))
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(gotInfo, wantInfo) {
		t.Fatalf("FrameworkDir = %q, want the repository's src/proxy_index", got)
	}
	for _, file := range []string{"schema.sql", filepath.Join("calculated", "schema.sql")} {
		if _, err := os.Stat(filepath.Join(got, file)); err != nil {
			t.Errorf("framework schema: %v", err)
		}
	}
}
