package config

import (
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"regexp"
	"strings"
	"syscall"

	"github.com/jackc/pgx/v5/pgconn"
)

// errPlaintextRemote is the error for a connection string whose TLS mode lets a
// connection to a host that is not local fall back to (or start in) plain text.
var errPlaintextRemote = errors.New("the database host is not local: sslmode must be require, verify-ca or verify-full")

var sslModeKeyword = regexp.MustCompile(`(^|\s)sslmode\s*=`)

// localHost reports whether every host in a libpq host list is this machine:
// a Unix socket folder, localhost or a loopback address.
func localHost(hosts string) bool {
	for _, host := range strings.Split(hosts, ",") {
		ip := net.ParseIP(host)
		if !strings.HasPrefix(host, "/") && host != "localhost" && (ip == nil || !ip.IsLoopback()) {
			return false
		}
	}
	return true
}

// requireTLS returns dsn when no connection it allows reaches a host that is
// not local without TLS. A dsn that names no sslmode (here or in PGSSLMODE)
// gets sslmode=require; one that names disable, allow or prefer is an error.
func requireTLS(dsn string) (string, error) {
	cfg, err := pgconn.ParseConfig(dsn)
	if err != nil {
		return "", fmt.Errorf("database connection string: %w", err)
	}
	if !plaintextRemote(cfg) {
		return dsn, nil
	}
	if os.Getenv("PGSSLMODE") != "" {
		return "", errPlaintextRemote
	}
	u, err := url.Parse(dsn)
	if err == nil && (u.Scheme == "postgres" || u.Scheme == "postgresql") {
		q := u.Query()
		if q.Has("sslmode") {
			return "", errPlaintextRemote
		}
		q.Set("sslmode", "require")
		u.RawQuery = q.Encode()
		return u.String(), nil
	}
	if sslModeKeyword.MatchString(dsn) {
		return "", errPlaintextRemote
	}
	return dsn + " sslmode=require", nil
}

// plaintextRemote reports whether one of the connections cfg tries (the first
// and its fallbacks) reaches a host that is not local without TLS.
func plaintextRemote(cfg *pgconn.Config) bool {
	if cfg.TLSConfig == nil && !localHost(cfg.Host) {
		return true
	}
	for _, f := range cfg.Fallbacks {
		if f.TLSConfig == nil && !localHost(f.Host) {
			return true
		}
	}
	return false
}

// openPrivate opens path for reading when it is a regular file (not a
// symbolic link) owned by the current user that no one else can read or
// write. The checks run on the open descriptor, so the file cannot be swapped
// between them and the read.
func openPrivate(path string) (*os.File, error) {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	info, err := f.Stat()
	switch {
	case err != nil:
	case !info.Mode().IsRegular():
		err = errors.New("not a regular file")
	case !ownedPrivate(info):
		err = errors.New("must be owned by the current user with mode 600")
	}
	if err != nil {
		f.Close()
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	return f, nil
}

// ownedPrivate reports whether the current user owns info's file and no one
// else has any access to it.
func ownedPrivate(info os.FileInfo) bool {
	st, ok := info.Sys().(*syscall.Stat_t)
	return ok && int(st.Uid) == os.Getuid() && info.Mode().Perm()&0o077 == 0
}
