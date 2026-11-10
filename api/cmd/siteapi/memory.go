package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/http/pprof"
	"os"
	"path/filepath"
	"runtime/debug"
	"strconv"
	"strings"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/memtrace"
)

// memLimitShare is the share of the unit's memory limit (cgroup v2
// memory.max) the Go runtime's soft limit is set to when GOMEMLIMIT is
// unset: the GC works harder before the kernel would kill (7 GB: 6.3 GB).
const memLimitShare = 0.9

// setMemoryLimit sets the Go soft memory limit to memLimitShare of the
// unit's limit, read under root ("/" in production), unless GOMEMLIMIT is
// set or the process has no limit. It returns the limit set (0: none).
func setMemoryLimit(root string) int64 {
	if os.Getenv("GOMEMLIMIT") != "" {
		return 0
	}
	max, ok := cgroupMemoryMax(root)
	if !ok {
		return 0
	}
	limit := int64(float64(max) * memLimitShare)
	debug.SetMemoryLimit(limit)
	slog.Info("go memory limit set from the unit's", "memory_max_mb", max>>20, "gomemlimit_mb", limit>>20)
	return limit
}

// cgroupMemoryMax is the memory.max of the process's cgroup v2 under root;
// false when there is none or it is "max".
func cgroupMemoryMax(root string) (int64, bool) {
	b, err := os.ReadFile(filepath.Join(root, "proc/self/cgroup"))
	if err != nil {
		return 0, false
	}
	for _, line := range strings.Split(string(b), "\n") {
		if path, ok := strings.CutPrefix(line, "0::"); ok {
			v, err := os.ReadFile(filepath.Join(root, "sys/fs/cgroup", path, "memory.max"))
			if err != nil {
				return 0, false
			}
			n, err := strconv.ParseInt(strings.TrimSpace(string(v)), 10, 64)
			return n, err == nil && n > 0
		}
	}
	return 0, false
}

// startMemoryTrace starts the memory trace of --mem-trace dir (none when
// ""): the RSS every 200 ms in dir/rss.tsv, each phase's start and the
// previous one's peak in the log, and the heap profile at the peak in
// dir/heap.pprof. It returns ctx carrying the trace and its stop.
func startMemoryTrace(ctx context.Context, dir string) (context.Context, func(), error) {
	if dir == "" {
		return ctx, func() {}, nil
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return ctx, nil, fmt.Errorf("memory trace: %w", err)
	}
	f, err := os.Create(filepath.Join(dir, "rss.tsv"))
	if err != nil {
		return ctx, nil, fmt.Errorf("memory trace: %w", err)
	}
	t := memtrace.Start(memtrace.Options{Samples: f, Heap: filepath.Join(dir, "heap.pprof"), Log: slog.Default()})
	return memtrace.With(ctx, t), func() { t.Stop(); f.Close() }, nil //nolint:errcheck // samples, best effort
}

// startPprof serves Go's profiles (net/http/pprof) on addr, which must be a
// loopback address; none when addr is "".
func startPprof(addr string) error {
	if addr == "" {
		return nil
	}
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return fmt.Errorf("pprof: %w", err)
	}
	if ip := net.ParseIP(host); host != "localhost" && (ip == nil || !ip.IsLoopback()) {
		return errors.New("pprof: the address must be loopback (127.0.0.1, ::1 or localhost)")
	}
	l, err := net.Listen("tcp", addr)
	if err != nil {
		return fmt.Errorf("pprof: %w", err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/debug/pprof/", pprof.Index)
	mux.HandleFunc("/debug/pprof/profile", pprof.Profile)
	mux.HandleFunc("/debug/pprof/symbol", pprof.Symbol)
	hs := &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go hs.Serve(l) //nolint:errcheck // ends with the process
	slog.Info("pprof listening", "addr", l.Addr().String())
	return nil
}
