package main

import (
	"context"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime/debug"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/memtrace"
)

// cgroupRoot is a root whose process is in cgroup /system.slice/api.service
// with memory.max max.
func cgroupRoot(t *testing.T, max string) string {
	root := t.TempDir()
	dir := filepath.Join(root, "sys/fs/cgroup/system.slice/api.service")
	for path, body := range map[string]string{filepath.Join(root, "proc/self/cgroup"): "0::/system.slice/api.service\n",
		filepath.Join(dir, "memory.max"): max + "\n"} {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func TestMemoryLimitIsFiveSixthsOfTheUnits(t *testing.T) {
	t.Setenv("GOMEMLIMIT", "")
	defer debug.SetMemoryLimit(debug.SetMemoryLimit(-1))
	if got := setMemoryLimit(cgroupRoot(t, "3221225472")); got != 2684354560 {
		t.Errorf("3 GiB unit: limit %d, want 2.5 GiB", got)
	}
	if got := debug.SetMemoryLimit(-1); got != 2684354560 {
		t.Errorf("the runtime's limit is %d", got)
	}
	for _, root := range []string{cgroupRoot(t, "max"), t.TempDir()} {
		if got := setMemoryLimit(root); got != 0 {
			t.Errorf("no unit limit: set %d", got)
		}
	}
	t.Setenv("GOMEMLIMIT", "1GiB")
	if got := setMemoryLimit(cgroupRoot(t, "3221225472")); got != 0 {
		t.Errorf("GOMEMLIMIT set: set %d", got)
	}
}

func TestPprofListensOnLoopbackOnly(t *testing.T) {
	for _, addr := range []string{"0.0.0.0:0", "10.0.0.1:6060", ":6060", "example.com:6060", "nonsense"} {
		if err := startPprof(addr); err == nil {
			t.Errorf("%s: served", addr)
		}
	}
	if err := startPprof(""); err != nil {
		t.Fatal(err)
	}
	if err := startPprof("127.0.0.1:16061"); err != nil {
		t.Skipf("loopback port busy: %v", err)
	}
	res, err := http.Get("http://127.0.0.1:16061/debug/pprof/heap?debug=1")
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	body, _ := io.ReadAll(res.Body)
	if res.StatusCode != http.StatusOK || !strings.Contains(string(body), "heap profile") {
		t.Errorf("heap profile: %d %.200s", res.StatusCode, body)
	}
}

func TestMemoryTraceWritesSamplesAndMarks(t *testing.T) {
	ctx, stop, err := startMemoryTrace(context.Background(), "")
	if err != nil || ctx != context.Background() {
		t.Fatalf("no directory: %v", err)
	}
	stop()
	dir := filepath.Join(t.TempDir(), "trace")
	ctx, stop, err = startMemoryTrace(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	memtrace.Mark(ctx, "load")
	stop()
	b, err := os.ReadFile(filepath.Join(dir, "rss.tsv"))
	if err != nil || !strings.Contains(string(b), "\tload\t") {
		t.Errorf("rss.tsv: %v %q", err, b)
	}
	if _, _, err := startMemoryTrace(context.Background(), filepath.Join(dir, "rss.tsv", "x")); err == nil {
		t.Error("a directory under a file: no error")
	}
}
