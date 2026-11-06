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
var FrameworkTree = filepath.Join("src", "proxy_index")

// Postgres is the [postgres] section of the repository's .secrets/*.toml.
type Postgres struct {
	Host     string `toml:"host"`
	Port     any    `toml:"port"`
	DBName   string `toml:"dbname"`
	User     string `toml:"user"`
	Password string `toml:"password"`
	SSLMode  string `toml:"sslmode"`
}

// ReadDatabaseFile parses a framework credentials TOML file.
func ReadDatabaseFile(path string) (Postgres, error) {
	var doc struct {
		Postgres *Postgres `toml:"postgres"`
	}
	if _, err := toml.DecodeFile(path, &doc); err != nil {
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
	if p.SSLMode == "" && (p.Host == "127.0.0.1" || p.Host == "localhost" || strings.HasPrefix(p.Host, "/")) {
		add("sslmode", "disable")
	}
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
func ResolveDSN(databaseURL, databaseFile string) (string, error) {
	if databaseURL != "" {
		return databaseURL, nil
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
	return p.DSN(), nil
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

// CI-API-1: an API-only change to measure tier1 (reverted next).
