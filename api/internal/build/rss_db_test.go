package build_test

import (
	"context"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// TestPeakRSS measures the peak RSS of a rebuild (LoadLists, then Build) of
// the testscale build at 18 networks x N requested countries as payload v4
// (testdb.PublishV4): 18 x N networks of one group each, every window's
// network list holding the group's rows. SITEAPI_RSS_COUNTRIES lists the N to
// measure (for example "20,40"); skipped without it. Each build is measured in
// a child process, so the generator's memory is not counted: two rebuilds, the
// second from the held lists, as serve does. The API server allows siteapi 3
// GB (MemoryMax) and the plan 2.5 GB at 18 x 249.
// Serial: a measurement, so nothing else in the package runs beside it.
func TestPeakRSS(t *testing.T) {
	if dsn := os.Getenv("SITEAPI_RSS_DSN"); dsn != "" {
		rssChild(t, dsn)
		return
	}
	shapes := os.Getenv("SITEAPI_RSS_COUNTRIES")
	if shapes == "" {
		t.Skip("set SITEAPI_RSS_COUNTRIES (for example 20,40) to measure the rebuild's peak RSS")
	}
	for _, s := range strings.Split(shapes, ",") {
		n, err := strconv.Atoi(s)
		if err != nil {
			t.Fatal(err)
		}
		db := testdb.New(t)
		windows := publishScale(t, db, n)
		cmd := exec.Command(os.Args[0], "-test.run=^TestPeakRSS$", "-test.v", "-test.timeout=20m")
		cmd.Env = append(os.Environ(), "SITEAPI_RSS_DSN="+db.DSN)
		out, err := cmd.CombinedOutput()
		for _, l := range strings.Split(string(out), "\n") {
			if strings.Contains(l, "rss:") {
				t.Logf("18 x %d (%d windows): %s", n, windows, strings.TrimSpace(l[strings.Index(l, "rss:"):]))
			}
		}
		if err != nil {
			t.Errorf("18 x %d: child %v\n%s", n, err, tail(string(out)))
		}
	}
}

func tail(s string) string {
	if len(s) > 2000 {
		return s[len(s)-2000:]
	}
	return s
}

func rssChild(t *testing.T, dsn string) {
	ctx := context.Background()
	pool, err := store.OpenReadOnly(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	lists := store.NewLists()
	for i := range 2 {
		t0 := time.Now()
		in, err := store.LoadLists(ctx, pool, lists)
		if err != nil {
			t.Fatal(err)
		}
		loaded := time.Since(t0)
		if _, err := build.Build(in); err != nil {
			t.Fatal(err)
		}
		t.Logf("rss: rebuild %d load %s build %s, VmHWM %s", i+1, loaded.Round(time.Millisecond),
			(time.Since(t0) - loaded).Round(time.Millisecond), hwm())
	}
}

func hwm() string {
	b, _ := os.ReadFile("/proc/self/status")
	for _, l := range strings.Split(string(b), "\n") {
		if strings.HasPrefix(l, "VmHWM") {
			return strings.Join(strings.Fields(l)[1:], " ")
		}
	}
	return "unknown"
}

// publishScale stores the 18 x n testscale build as payload v4 and returns
// the window count.
func publishScale(t *testing.T, db *testdb.DB, n int) int {
	t.Helper()
	shape := testscale.QA1
	shape.Countries = n
	in, err := testscale.Inputs(shape)
	if err != nil {
		t.Fatal(err)
	}
	db.PublishV4(t, in.Snapshot.Payload)
	db.Exec(t, "ANALYZE calculated_list_rows")
	return 3 * shape.Networks * n
}
