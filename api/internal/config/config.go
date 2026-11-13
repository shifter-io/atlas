// Package config resolves database connection settings.
package config

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/BurntSushi/toml"
)

// FrameworkTree is the framework's source tree (the Python package with its
// schema files), relative to the repository root. RepoRoot finds the root by it.
var FrameworkTree = filepath.Join("src", "atlas")

// Postgres is the [postgres] section of the repository's .secrets/*.toml.
type Postgres struct {
	Host     string `toml:"host"`
	Port     any    `toml:"port"`
	DBName   string `toml:"dbname"`
	User     string `toml:"user"`
	Password string `toml:"password"`
	SSLMode  string `toml:"sslmode"`
	// SSLRootCert is the CA file that verify-ca and verify-full check the server against.
	SSLRootCert string `toml:"sslrootcert"`
}

// ReadDatabaseFile parses a framework credentials TOML file. Like the Python
// side, it refuses a file that is not a regular file owned by the current
// user with no access for others (mode 600).
func ReadDatabaseFile(path string) (Postgres, error) {
	var doc struct {
		Postgres *Postgres `toml:"postgres"`
	}
	f, err := openPrivate(path)
	if err != nil {
		return Postgres{}, err
	}
	defer f.Close()
	if _, err := toml.NewDecoder(f).Decode(&doc); err != nil {
		return Postgres{}, fmt.Errorf("read %s: %w", path, err)
	}
	if doc.Postgres == nil {
		return Postgres{}, fmt.Errorf("%s has no [postgres] section", path)
	}
	return *doc.Postgres, nil
}

func quote(v string) string {
	return "'" + strings.NewReplacer(`\`, `\\`, `'`, `\'`).Replace(v) + "'"
}

// DSN renders a libpq keyword/value connection string.
func (p Postgres) DSN() string {
	parts := []string{}
	add := func(k, v string) {
		if v != "" {
			parts = append(parts, k+"="+quote(v))
		}
	}
	add("host", p.Host)
	if p.Port != nil {
		add("port", fmt.Sprint(p.Port))
	}
	add("dbname", p.DBName)
	add("user", p.User)
	add("password", p.Password)
	add("sslmode", p.SSLMode)
	if p.SSLMode == "" && p.Host != "" {
		mode := "require"
		if localHost(p.Host) {
			mode = "disable"
		}
		add("sslmode", mode)
	}
	add("sslrootcert", p.SSLRootCert)
	add("connect_timeout", "5")
	return strings.Join(parts, " ")
}

// WithDB returns a copy pointing at another database.
func (p Postgres) WithDB(name string) Postgres {
	p.DBName = name
	return p
}

// ResolveDSN picks DATABASE_URL, an explicit --database-file, or the
// default credentials file .secrets/database.toml in the repository root.
// Either way a host that is not local must use TLS (see requireTLS).
func ResolveDSN(databaseURL, databaseFile string) (string, error) {
	if databaseURL != "" {
		return requireTLS(databaseURL)
	}
	if databaseFile == "" {
		databaseFile = RepoPath(".secrets", "database.toml")
	}
	if databaseFile == "" {
		return "", errors.New("no database configured: set DATABASE_URL or --database-file, or add .secrets/database.toml to the repository root")
	}
	p, err := ReadDatabaseFile(databaseFile)
	if err != nil {
		return "", err
	}
	return requireTLS(p.DSN())
}

// RepoRoot returns the repository root: the working directory or its nearest
// parent that holds FrameworkTree, or "" when there is none.
func RepoRoot() string {
	return findDir(FrameworkTree)
}

// RepoPath returns <repository root>/<parts...> when it exists, else "".
func RepoPath(parts ...string) string {
	root := RepoRoot()
	if root == "" {
		return ""
	}
	path := filepath.Join(append([]string{root}, parts...)...)
	if _, err := os.Stat(path); err != nil {
		return ""
	}
	return path
}

// FindUp looks for a relative path in the working directory and its parents.
func FindUp(rel string) string {
	dir := findDir(rel)
	if dir == "" {
		return ""
	}
	return filepath.Join(dir, rel)
}

// findDir returns the working directory or its nearest parent that holds the
// relative path rel, or "" when there is none.
func findDir(rel string) string {
	dir, err := os.Getwd()
	if err != nil {
		return ""
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, rel)); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return ""
		}
		dir = parent
	}
}
