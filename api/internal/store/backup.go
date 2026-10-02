package store

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

// BackupResult describes a pg_dump written before a destructive command.
type BackupResult struct {
	Path     string    `json:"path"`
	Bytes    int64     `json:"bytes"`
	SHA256   string    `json:"sha256"`
	Database string    `json:"database"`
	TakenAt  time.Time `json:"taken_at"`
	Restore  string    `json:"restore"`
}

// PgBinDir reads bin_dir from milf/.state/postgres/runtime.json.
func PgBinDir(runtimeFile string) (string, error) {
	body, err := os.ReadFile(runtimeFile)
	if err != nil {
		return "", err
	}
	var rt struct {
		BinDir string `json:"bin_dir"`
	}
	if err := json.Unmarshal(body, &rt); err != nil {
		return "", fmt.Errorf("%s: %w", runtimeFile, err)
	}
	if rt.BinDir == "" {
		return "", fmt.Errorf("%s has no bin_dir", runtimeFile)
	}
	return rt.BinDir, nil
}

// Backup writes `pg_dump -Fc` of the configured database into
// <dir>/<UTC stamp>_<label>/database.dump (mode 0600), checks it with
// `pg_restore --list`, and records a manifest next to it.
func Backup(dsn, binDir, dir, label string) (*BackupResult, error) {
	cfg, err := pgx.ParseConfig(dsn)
	if err != nil {
		return nil, err
	}
	stamp := time.Now().UTC()
	folder := filepath.Join(dir, stamp.Format("20060102_150405")+"_"+label)
	if err := os.MkdirAll(folder, 0o700); err != nil {
		return nil, err
	}
	dump := filepath.Join(folder, "database.dump")
	cmd := exec.Command(filepath.Join(binDir, "pg_dump"),
		"-h", cfg.Host, "-p", strconv.Itoa(int(cfg.Port)), "-U", cfg.User, "-d", cfg.Database, "-Fc", "-f", dump)
	cmd.Env = append(os.Environ(), "PGPASSWORD="+cfg.Password, "PGCONNECT_TIMEOUT=10")
	if out, err := cmd.CombinedOutput(); err != nil {
		return nil, fmt.Errorf("pg_dump: %v: %s", err, out)
	}
	if err := os.Chmod(dump, 0o600); err != nil {
		return nil, err
	}
	if out, err := exec.Command(filepath.Join(binDir, "pg_restore"), "--list", dump).CombinedOutput(); err != nil {
		return nil, fmt.Errorf("pg_restore --list rejected the dump: %v: %s", err, out)
	}
	f, err := os.Open(dump)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		return nil, err
	}
	res := &BackupResult{
		Path: dump, Bytes: n, SHA256: hex.EncodeToString(h.Sum(nil)), Database: cfg.Database, TakenAt: stamp,
		Restore: fmt.Sprintf("%s --clean --if-exists --no-owner -h %s -p %d -U %s -d %s %s",
			shellQuote(filepath.Join(binDir, "pg_restore")), cfg.Host, cfg.Port, shellQuote(cfg.User), shellQuote(cfg.Database), shellQuote(dump)),
	}
	manifest, _ := json.MarshalIndent(res, "", "  ")
	if err := os.WriteFile(filepath.Join(folder, "backup.json"), append(manifest, '\n'), 0o600); err != nil {
		return nil, err
	}
	return res, nil
}

func shellQuote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'"
}
