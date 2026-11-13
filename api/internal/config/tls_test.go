package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// A host that is not local gets sslmode=require when the connection string
// names no mode, and may not name one that allows plain text. Local hosts and
// sockets keep their mode.
func TestResolveDSNRequiresTLSForARemoteHost(t *testing.T) {
	t.Setenv("PGSSLMODE", "")
	for _, c := range []struct{ in, want string }{
		{"postgres://u@db.example/d", "postgres://u@db.example/d?sslmode=require"},
		{"postgres://u@localhost,db.example/d", "postgres://u@localhost,db.example/d?sslmode=require"},
		{"host=db.example dbname=d", "host=db.example dbname=d sslmode=require"},
		{"postgres://u@db.example/d?sslmode=require", "postgres://u@db.example/d?sslmode=require"},
		{"host=db.example sslmode=verify-ca", "host=db.example sslmode=verify-ca"},
		{"postgres://u@127.0.0.1/d?sslmode=disable", "postgres://u@127.0.0.1/d?sslmode=disable"},
		{"postgres://u@[::1]/d", "postgres://u@[::1]/d"},
		{"host=localhost sslmode=prefer", "host=localhost sslmode=prefer"},
		{"host=/run/postgresql dbname=d", "host=/run/postgresql dbname=d"},
	} {
		if got, err := ResolveDSN(c.in, ""); err != nil || got != c.want {
			t.Errorf("%q: %q %v, want %q", c.in, got, err, c.want)
		}
	}
	for _, in := range []string{
		"postgres://u@db.example/d?sslmode=disable",
		"postgres://u@db.example/d?sslmode=allow",
		"postgres://u@127.0.0.1,db.example/d?sslmode=prefer",
		"host=db.example sslmode=prefer",
		"host=db.example sslmode = disable",
	} {
		if got, err := ResolveDSN(in, ""); err == nil || !strings.Contains(err.Error(), "not local") {
			t.Errorf("%q: %q %v, want the plain-text mode refused", in, got, err)
		}
	}
	if _, err := ResolveDSN("postgres://u@db.example:x/d", ""); err == nil || !strings.Contains(err.Error(), "database connection string") {
		t.Errorf("an unparsable URL: %v", err)
	}
}

// PGSSLMODE chooses the mode when the string does not, so a plain-text mode
// there is refused, not overridden.
func TestResolveDSNRefusesAPlaintextPGSSLMODE(t *testing.T) {
	t.Setenv("PGSSLMODE", "disable")
	if got, err := ResolveDSN("host=db.example dbname=d", ""); err == nil {
		t.Fatalf("PGSSLMODE=disable for a remote host: %q", got)
	}
	if _, err := ResolveDSN("host=127.0.0.1 dbname=d", ""); err != nil {
		t.Fatalf("PGSSLMODE=disable for a local host: %v", err)
	}
}

// The same rules hold for a credentials file.
func TestResolveDSNFileRequiresTLSForARemoteHost(t *testing.T) {
	t.Setenv("PGSSLMODE", "")
	dir := t.TempDir()
	write := func(name, body string) string {
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, []byte("[postgres]\n"+body), 0o600); err != nil {
			t.Fatal(err)
		}
		return path
	}
	if got, err := ResolveDSN("", write("a.toml", "host = \"db.example\"\n")); err != nil || !strings.Contains(got, "sslmode='require'") {
		t.Fatalf("a remote host without sslmode: %q %v", got, err)
	}
	if got, err := ResolveDSN("", write("b.toml", "host = \"db.example\"\nsslmode = \"disable\"\n")); err == nil {
		t.Fatalf("a remote host with sslmode=disable: %q", got)
	}
	if got, err := ResolveDSN("", write("c.toml", "host = \"127.0.0.1\"\n")); err != nil || !strings.Contains(got, "sslmode='disable'") {
		t.Fatalf("a local host: %q %v", got, err)
	}
}

// ReadDatabaseFile reads only a regular file that others cannot access.
func TestReadDatabaseFileMustBePrivate(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "database.toml")
	if err := os.WriteFile(path, []byte("[postgres]\nhost = \"127.0.0.1\"\nsslrootcert = \"ca.pem\"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if p, err := ReadDatabaseFile(path); err != nil || p.SSLRootCert != "ca.pem" {
		t.Fatalf("a private file: %+v %v", p, err)
	}
	link := filepath.Join(dir, "link.toml")
	if err := os.Symlink(path, link); err != nil {
		t.Fatal(err)
	}
	for _, bad := range []string{link, dir} {
		if _, err := ReadDatabaseFile(bad); err == nil || !strings.Contains(err.Error(), "read "+bad) {
			t.Errorf("%s: %v, want it refused", bad, err)
		}
	}
	if _, err := ReadDatabaseFile(dir); err == nil || !strings.Contains(err.Error(), "not a regular file") {
		t.Errorf("a folder: %v", err)
	}
	if err := os.Chmod(path, 0o640); err != nil {
		t.Fatal(err)
	}
	if _, err := ReadDatabaseFile(path); err == nil || !strings.Contains(err.Error(), "mode 600") {
		t.Errorf("a group-readable file: %v", err)
	}
}
