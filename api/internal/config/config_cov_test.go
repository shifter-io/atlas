package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestReadDatabaseFileWithoutPostgresSection(t *testing.T) {
	path := filepath.Join(t.TempDir(), "other.toml")
	if err := os.WriteFile(path, []byte("[mysql]\nhost = \"db\"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := ReadDatabaseFile(path)
	if err == nil || !strings.Contains(err.Error(), "has no [postgres] section") {
		t.Fatalf("err = %v, want the missing section named", err)
	}
	if err := os.WriteFile(path, []byte("[postgres\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := ReadDatabaseFile(path); err == nil || !strings.Contains(err.Error(), "read "+path) {
		t.Fatalf("err = %v, want the file named", err)
	}
}

// WithDB changes only the database name, on a copy.
func TestWithDBCopies(t *testing.T) {
	p := Postgres{Host: "db.example", Port: 5433, DBName: "site", User: "u", SSLMode: "require"}
	q := p.WithDB("siteapi_test_x")
	if p.DBName != "site" {
		t.Fatalf("WithDB changed the original: %+v", p)
	}
	if want := "host='db.example' port='5433' dbname='siteapi_test_x' user='u' sslmode='require' connect_timeout='5'"; q.DSN() != want {
		t.Fatalf("DSN %q, want %q", q.DSN(), want)
	}
}

// DSN quotes values, skips empty ones and turns SSL off only for a local host
// that does not choose a mode.
func TestDSNQuotingAndLocalSSL(t *testing.T) {
	for _, c := range []struct {
		p    Postgres
		want string
	}{
		{Postgres{Host: "localhost", Password: `it's\x`}, `host='localhost' password='it\'s\\x' sslmode='disable' connect_timeout='5'`},
		{Postgres{Host: "/run/postgresql", Port: "5432"}, `host='/run/postgresql' port='5432' sslmode='disable' connect_timeout='5'`},
		{Postgres{Host: "127.0.0.1", SSLMode: "verify-full"}, `host='127.0.0.1' sslmode='verify-full' connect_timeout='5'`},
		{Postgres{Host: "db.example"}, `host='db.example' connect_timeout='5'`},
		{Postgres{}, `connect_timeout='5'`},
	} {
		if got := c.p.DSN(); got != c.want {
			t.Errorf("%+v: %q, want %q", c.p, got, c.want)
		}
	}
}

// From a working directory that no longer exists there is no repository.
func TestFindDirWithoutWorkingDirectory(t *testing.T) {
	gone := filepath.Join(t.TempDir(), "gone")
	if err := os.Mkdir(gone, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Chdir(gone)
	if err := os.Remove(gone); err != nil {
		t.Fatal(err)
	}
	if wd, err := os.Getwd(); err == nil {
		t.Fatalf("Getwd of a removed folder = %q, want an error", wd)
	}
	if got := RepoRoot(); got != "" {
		t.Fatalf("RepoRoot = %q, want empty", got)
	}
	if got := FindUp("x"); got != "" {
		t.Fatalf("FindUp = %q, want empty", got)
	}
}
