package build_test

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/memtrace"
	"github.com/Zeus-Development/atlas/api/internal/server"
	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/Zeus-Development/atlas/api/internal/testdb"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// peakLimit is the most RSS a build may reach at the 7-day volume: production's
// unit allows the siteapi 7 GB (MemoryMax), and a build peaks at most 3 GB,
// for 30 days of data and growth.
const peakLimit = 3_000_000_000

// TestPeakRSS measures the siteapi's memory on testscale.Week as a v4 build
// (SITEAPI-MEM-1), as `siteapi serve` builds it: a server.Server reading
// through a read-only pool, with the default row cache. A child process
// builds, so the generator's memory is not counted, and samples its RSS every
// 200 ms (memtrace) in each phase of the serve path. It makes the first
// build; then the next hour's build is published (every answer changes:
// its as-of is an hour later) and it rebuilds while the first version serves,
// so the two overlap. Each phase's peak is logged (with SITEAPI_RSS_HEAP, the
// heap profile at the peak is written there); either build above peakLimit
// fails. Serial: a measurement, so nothing else in the package runs beside it.
func TestPeakRSS(t *testing.T) {
	if dsn := os.Getenv("SITEAPI_RSS_DSN"); dsn != "" {
		rssChild(t, dsn)
		return
	}
	db := testdb.New(t)
	if testing.Short() {
		t.Skip("publishes a production week twice")
	}
	publishWeek(t, db)
	cmd := exec.Command(os.Args[0], "-test.run=^TestPeakRSS$", "-test.v", "-test.timeout=10m")
	cmd.Env = append(os.Environ(), "SITEAPI_RSS_DSN="+db.DSN)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	stdin, err := cmd.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	peaks, out := readChild(t, stdout, func() {
		nextHour(t, db)
		fmt.Fprintln(stdin)
	})
	if err := cmd.Wait(); err != nil || len(peaks) != 2 {
		t.Fatalf("child: %v, peaks %v\n%s%s", err, peaks, tail(out), tail(stderr.String()))
	}
	for i, name := range []string{"first build", "rebuild"} {
		if peaks[i] > peakLimit {
			t.Errorf("%s: peak RSS %d MB, over the %d MB limit", name, peaks[i]>>20, peakLimit>>20)
		}
	}
}

// readChild logs the child's "rss:" lines and returns its peaks (the
// "rss-peak" lines, in bytes) and its whole output; at its "rss-ready" line
// it calls next, which must answer on the child's stdin.
func readChild(t *testing.T, stdout io.Reader, next func()) ([]uint64, string) {
	var peaks []uint64
	var out strings.Builder
	sc := bufio.NewScanner(stdout)
	for sc.Scan() {
		l := sc.Text()
		out.WriteString(l + "\n")
		switch f := strings.Fields(l); {
		case len(f) == 2 && f[0] == "rss-peak":
			n, err := strconv.ParseUint(f[1], 10, 64)
			if err != nil {
				t.Fatal(err)
			}
			peaks = append(peaks, n)
		case l == "rss-ready":
			next()
		case strings.Contains(l, "rss:"):
			t.Log(strings.TrimSpace(l[strings.Index(l, "rss:"):]))
		}
	}
	return peaks, out.String()
}

func tail(s string) string {
	if len(s) > 4000 {
		return s[len(s)-4000:]
	}
	return s
}

// rssChild builds the week as serve does, waits for the next build between
// its first build and its rebuild, and prints each phase and each build's
// peak.
func rssChild(t *testing.T, dsn string) {
	ctx := context.Background()
	pool, err := store.OpenReadOnly(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	tr := memtrace.Start(memtrace.Options{Heap: os.Getenv("SITEAPI_RSS_HEAP")})
	ctx = memtrace.With(ctx, tr)
	s := server.New(server.DBSource{Pool: pool, Lists: &store.Lists{RowCacheBytes: store.DefaultRowCacheBytes}},
		server.Options{Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), FilesDir: t.TempDir()})
	refresh(ctx, t, s)
	tr.Mark("next build published")
	fmt.Println("rss-ready")
	if _, err := bufio.NewReader(os.Stdin).ReadString('\n'); err != nil {
		t.Fatal(err)
	}
	tr.Mark("rebuild")
	first := s.Current()
	refresh(ctx, t, s)
	tr.Stop()
	reused := 0
	for path, r := range s.Current().Static {
		if first.Static[path] == r {
			reused++
		}
	}
	fmt.Printf("rss: the rebuild reused %d of its %d bodies\n", reused, len(first.Static))
	if reused > len(first.Static)/10 {
		t.Fatalf("the rebuild reused %d of %d bodies: it does not measure a new hour", reused, len(first.Static))
	}
	var peaks [2]uint64
	i := 0
	for _, p := range tr.Phases() {
		fmt.Printf("rss: %-24s at %6.1f s: %5d MB at its start, %5d MB peak\n", p.Name, p.At.Seconds(), p.StartRSS>>20, p.Peak>>20)
		if p.Name == "rebuild" {
			i = 1
		}
		if p.Name != "next build published" {
			peaks[i] = max(peaks[i], p.Peak)
		}
	}
	fmt.Printf("rss: first build peak %d MB, rebuild peak %d MB (limit %d MB)\n", peaks[0]>>20, peaks[1]>>20, peakLimit>>20)
	fmt.Printf("rss-peak %d\nrss-peak %d\n", peaks[0], peaks[1])
}

// refresh makes s build the current build and prints the build's times.
func refresh(ctx context.Context, t *testing.T, s *server.Server) {
	start := time.Now()
	if built, err := s.Refresh(ctx, false); err != nil || !built {
		t.Fatalf("built %v: %v", built, err)
	}
	v := s.Current()
	fmt.Printf("rss: build %d in %v (load %v, assemble %v, render %v)\n", v.ID, time.Since(start).Round(time.Millisecond),
		v.LoadTime.Round(time.Millisecond), v.AssembleTime.Round(time.Millisecond), v.RenderTime.Round(time.Millisecond))
}

// publishWeek publishes testscale.Week as a v4 build with its targets and
// events.
func publishWeek(t *testing.T, db *testdb.DB) {
	t.Helper()
	start := time.Now()
	week := testscale.Week
	payload, err := week.Payload()
	if err != nil {
		t.Fatal(err)
	}
	db.Exec(t, week.TargetsSQL())
	db.Exec(t, week.EventsSQL())
	db.PublishV4With(t, payload, testdb.V4Lists{Requested: week.Requested, Rows: week.Lists()})
	db.Exec(t, "VACUUM ANALYZE calculated_list_rows")
	t.Logf("published the week in %v", time.Since(start).Round(time.Millisecond))
}

// nextHour publishes the current build again an hour later, reusing every
// list, as the hourly calculation does: every as-of time moves an hour, so
// every answer changes.
func nextHour(t *testing.T, db *testdb.DB) {
	t.Helper()
	db.Republish(t, func(head []byte) []byte {
		var h struct {
			AsOf string `json:"as_of"`
		}
		if err := json.Unmarshal(head, &h); err != nil {
			t.Fatal(err)
		}
		asOf, err := time.Parse(time.RFC3339, h.AsOf)
		if err != nil {
			t.Fatal(err)
		}
		later := asOf.Add(time.Hour).Format("2006-01-02T15:04:05-07:00")
		return bytes.ReplaceAll(head, []byte(`"`+h.AsOf+`"`), []byte(`"`+later+`"`))
	})
}
