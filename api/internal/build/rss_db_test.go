package build_test

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index-website/api/internal/build"
	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
	"github.com/Zeus-Development/proxy-index-website/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index-website/api/internal/testscale"
)

// TestPeakRSS measures the peak RSS of a rebuild (LoadLists, then Build) of
// the testscale build at 18 networks x N requested countries, stored as the
// framework will store it: payload v3 in parts over K2 lists, one list per
// window with every row (country 240, region, city, ASN, country x ASN and
// city x ASN 200 each). SITEAPI_RSS_COUNTRIES lists the N to measure (for
// example "20,40"); skipped without it. Each build is measured in a child
// process, so the generator's memory is not counted: two rebuilds, the second
// from the held lists, as serve does. The API server allows siteapi 3 GB
// (MemoryMax) and the plan 2.5 GB at 18 x 249.
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
		t.Logf("rss: rebuild %d load %s build %s, lists fetched %d, VmHWM %s", i+1, loaded.Round(time.Millisecond),
			(time.Since(t0) - loaded).Round(time.Millisecond), lists.Fetched, hwm())
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

// publishScale stores the 18 x n testscale build as payload v3 parts over
// K2 lists and returns the window count.
func publishScale(t *testing.T, db *testdb.DB, n int) int {
	t.Helper()
	shape := testscale.QA1
	shape.Countries = n
	in, err := testscale.Inputs(shape)
	if err != nil {
		t.Fatal(err)
	}
	head, products, runs, err := testscale.Parts(in.Snapshot.Payload, true)
	if err != nil {
		t.Fatal(err)
	}
	in = nil
	id := 0
	for i, raw := range products {
		var p map[string]any
		if err := json.Unmarshal(raw, &p); err != nil {
			t.Fatal(err)
		}
		for _, entry := range p["windows"].(map[string]any) {
			w := entry.(map[string]any)
			kept := map[string]int{}
			for name, rows := range w["breakdowns"].(map[string]any) {
				kept[name] = len(rows.([]any))
			}
			pool := w["pool"].(map[string]any)
			for _, name := range []string{"geo", "countries_breakdown", "asns_breakdown"} {
				kept[name] = len(pool[name].([]any))
				delete(pool, name)
			}
			delete(w, "breakdowns")
			id++
			w["lists"] = map[string]any{"id": id, "entries": kept}
		}
		if products[i], err = json.Marshal(p); err != nil {
			t.Fatal(err)
		}
	}
	db.PublishParts(t, head, products, runs)
	db.Exec(t, fmt.Sprintf(`INSERT INTO place_names(country_code,country,region_code,region,city)
  SELECT 'C'||i%%250, 'Country '||i%%250, 'R'||i%%40, 'Region '||i%%40, 'City '||i FROM generate_series(0,999) i;
 INSERT INTO asn_names(asn,name) SELECT 64512+i, 'Example Networks '||i FROM generate_series(0,999) i;
 INSERT INTO asn_labels(asn_name_id,usage_type) SELECT asn_name_id,'isp' FROM asn_names;
 INSERT INTO calculated_lists(`+testdb.ListColumns+`) SELECT 1,'w'||i,1,`+testdb.ListFill+` FROM generate_series(1,%[1]d) i;
 INSERT INTO calculated_list_rows SELECT l.list_id, d.code, p, CASE WHEN d.code <> 'a' THEN 1+(p+l.list_id)%%1000 END,
  CASE WHEN d.code IN ('a','x','y') THEN 1+(p+l.list_id)%%1000 END, 1000-p, 2000-2*p, 3000-3*p, 2000-2*p, 0, 0, 400+p, 900+p, 2000+p
  FROM calculated_lists l, (VALUES ('c'::"char",%[2]d),('r',%[3]d),('g',%[3]d),('a',%[3]d),('x',%[3]d),('y',%[3]d)) d(code,n), generate_series(0,d.n-1) p;
 ANALYZE calculated_list_rows`, id, shape.Exits, shape.Tail))
	return id
}
