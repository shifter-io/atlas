package store

import (
	"bytes"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"
)

var day = time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)

// point is a series point with data, as jsonb reads it back.
func point(at time.Time, completed int) string {
	return fmt.Sprintf(`{"at": "%s", "uptime": 1, "completed": %d, "successes": %d, "latency_ms": {"p50": 400, "p90": 900, "p95": null, "p99": 2000}, "unique_ips": %d, "success_rate": 0.5}`,
		pyISO(at), completed, completed/2, completed)
}

// daySeries is a 24-hour series from start: the buckets in data have a point
// with data, the others are empty, and only those with data are kept when sparse.
func daySeries(start time.Time, data map[int]bool, timed, sparse bool) string {
	points := []string{}
	for i := 0; start.Add(time.Duration(i) * bucket).Before(start.Add(24 * time.Hour)); i++ {
		at := start.Add(time.Duration(i) * bucket)
		switch {
		case data[i]:
			points = append(points, point(at, 3+i))
		case !sparse:
			points = append(points, emptyPoint(pyISO(at), timed))
		}
	}
	return "[" + strings.Join(points, ", ") + "]"
}

func window(start time.Time, hours int, latency, series string) string {
	end := start.Add(time.Duration(hours) * time.Hour)
	return fmt.Sprintf(`{"pool": {"unique_ips": 7}, "period": {"end": "%s", "hours": %d, "start": "%s", "boundary": "[start,end)"}, "series": %s, "latency": %s, "readiness": "ready"}`,
		pyISO(end), hours, pyISO(start), series, latency)
}

func group(windows ...string) string {
	return `{"id": "1:daily-us", "kind": "residential", "cohort": "daily-us", "windows": {` + strings.Join(windows, ", ") + `}, "product_id": 1}`
}

func TestDenseGivesASparseDaySeriesEveryBucketBack(t *testing.T) {
	t.Parallel()
	data := map[int]bool{0: true, 7: true, 287: true}
	week := window(day.Add(-6*24*time.Hour), 168, `null`, `[`+point(day, 3)+`]`) // a 7d series is never refilled
	for _, c := range []struct {
		name, latency string
		timed         bool
		start         time.Time
	}{
		{"timed", `{"target": "generic_http"}`, true, day},
		{"no target", `{"target": null}`, false, day},
		{"empty target", `{"target": ""}`, false, day},
		{"no latency block", `null`, false, day},
		{"microseconds", `{"target": "generic_http"}`, true, day.Add(1234567 * time.Microsecond)},
	} {
		t.Run(c.name, func(t *testing.T) {
			whole := group(`"24h": `+window(c.start, 24, c.latency, daySeries(c.start, data, c.timed, false)), `"7d": `+week)
			sparse := group(`"24h": `+window(c.start, 24, c.latency, daySeries(c.start, data, c.timed, true)), `"7d": `+week)
			got, err := dense([]byte(sparse))
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != whole {
				t.Fatalf("dense:\n%.600s\nwant\n%.600s", got, whole)
			}
			if n := strings.Count(string(got), `"latency_under_load_ms"`); (n > 0) != c.timed || (c.timed && n != 288-len(data)) {
				t.Errorf("%d points with latency_under_load_ms", n)
			}
		})
	}
}

func TestEmptyPointIsWhatTheFrameworkWrites(t *testing.T) {
	t.Parallel()
	// proxy-index aggregate.series() for a bucket without rows, keys in jsonb order.
	want := `{"at": "2026-11-01T00:05:00+00:00", "uptime": null, "completed": 0, "successes": 0, ` +
		`"latency_ms": {"p50": null, "p90": null, "p95": null, "p99": null}, "unique_ips": null, "success_rate": null, ` +
		`"latency_under_load_ms": {"p50": null, "p90": null, "p95": null, "p99": null}}`
	if got := emptyPoint(pyISO(day.Add(bucket)), true); got != want {
		t.Errorf("%s", got)
	}
	if got := emptyPoint("x", false); strings.Contains(got, "under_load") {
		t.Errorf("untimed %s", got)
	}
}

func TestDenseLeavesWholeAndOtherSeriesAsTheyAre(t *testing.T) {
	t.Parallel()
	whole := window(day, 24, `{"target": "generic_http"}`, daySeries(day, map[int]bool{3: true}, true, false))
	gappy := window(day, 168, `null`, `[`+point(day, 3)+`]`)
	for name, product := range map[string]string{
		"whole 24h":    group(`"24h": ` + whole),
		"7d with gaps": group(`"7d": ` + gappy),
		"no windows":   `{"id": "1:x", "product_id": 1}`,
		"no series":    group(`"24h": {"period": {"hours": 24, "start": "x", "end": "y"}}`),
		"no hours":     group(`"24h": {"period": {"start": "x"}, "series": []}`),
	} {
		in := []byte(product)
		got, err := dense(in)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if &got[0] != &in[0] || string(got) != product {
			t.Errorf("%s: changed or copied: %s", name, got)
		}
	}
}

func TestDenseRefusesWhatItCannotRefill(t *testing.T) {
	t.Parallel()
	timed := `{"target": "generic_http"}`
	off := window(day, 24, timed, `[`+point(day.Add(time.Minute), 3)+`]`)
	twice := window(day, 24, timed, `[`+point(day, 3)+`, `+point(day, 4)+`]`)
	tooMany := window(day, 24, timed, strings.Replace(daySeries(day, nil, true, false), "[", "["+point(day, 3)+", ", 1))
	for name, c := range map[string]struct{ product, want string }{
		"off the grid":          {group(`"24h": ` + off), "1 series points off the 5m0s grid"},
		"two in one bucket":     {group(`"24h": ` + twice), "two series points share a bucket"},
		"more than buckets":     {group(`"24h": ` + tooMany), "two series points share a bucket"},
		"bad period":            {group(`"24h": {"period": {"hours": 24, "start": "x", "end": "y"}, "series": []}`), `period "x" to "y"`},
		"series not a list":     {group(`"24h": {"period": {"hours": 24}, "series": {}}`), "cannot unmarshal"},
		"point without at":      {group(`"24h": ` + window(day, 24, timed, `[1]`)), "cannot unmarshal"},
		"windows not an object": {`{"windows": []}`, "not a JSON object"},
		"window not an object":  {group(`"24h": 3`), "window 24h: not a JSON object"},
		"not an object":         {`[]`, "not a JSON object"},
	} {
		if _, err := dense([]byte(c.product)); err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: %v, want %q", name, err, c.want)
		}
	}
}

func TestObjectFieldsSpanTheRawValues(t *testing.T) {
	t.Parallel()
	raw := []byte(` { "a" : [1, {"x": 2}] ,"b\"c":"d",  "e": null } `)
	fields, err := objectFields(raw)
	if err != nil {
		t.Fatal(err)
	}
	got := []string{}
	for _, f := range fields {
		got = append(got, f.Key+"="+string(raw[f.Start:f.End]))
	}
	if strings.Join(got, "|") != `a=[1, {"x": 2}]|b"c="d"|e=null` {
		t.Errorf("%q", got)
	}
	for _, bad := range []string{``, `[1]`, `{"a": }`, `{"a": 1`, `{1: 2}`} {
		if _, err := objectFields([]byte(bad)); err == nil {
			t.Errorf("%q: no error", bad)
		}
	}
}

func TestWithProductsPutsTheGroupsWhereJsonbOrdersThem(t *testing.T) {
	t.Parallel()
	two := func(out *bytes.Buffer) error { out.WriteString(`{"id": 1}, {"id": 2}`); return nil }
	none := func(*bytes.Buffer) error { return nil }
	for head, want := range map[string]string{
		`{}`:                 `{"products": [{"id": 1}, {"id": 2}]}`,
		`{"as_of": "t"}`:     `{"as_of": "t", "products": [{"id": 1}, {"id": 2}]}`,
		`{"overlap": []}`:    `{"overlap": [], "products": [{"id": 1}, {"id": 2}]}`,
		`{"protocol": 1}`:    `{"products": [{"id": 1}, {"id": 2}], "protocol": 1}`,
		`{"readiness": "r"}`: `{"products": [{"id": 1}, {"id": 2}], "readiness": "r"}`,
		`{"fleet": [], "produces": 1, "protocol": 2, "snapshot_id": "s"}`: `{"fleet": [], "produces": 1, "products": [{"id": 1}, {"id": 2}], "protocol": 2, "snapshot_id": "s"}`,
	} {
		got, err := withProducts([]byte(head), two)
		if err != nil || string(got) != want {
			t.Errorf("%s: %s %v", head, got, err)
		}
	}
	if got, err := withProducts([]byte(`{"as_of": "t"}`), none); err != nil || string(got) != `{"as_of": "t", "products": []}` {
		t.Errorf("no groups: %s %v", got, err)
	}
	if _, err := withProducts([]byte(`{"products": []}`), none); err == nil || !strings.Contains(err.Error(), "head holds products") {
		t.Errorf("head with products: %v", err)
	}
	if _, err := withProducts([]byte(`[]`), none); err == nil {
		t.Error("head not an object: no error")
	}
	stop := errors.New("stop")
	if _, err := withProducts([]byte(`{}`), func(*bytes.Buffer) error { return stop }); !errors.Is(err, stop) {
		t.Errorf("groups error: %v", err)
	}
}

func TestJsonbOrderAndPythonStamps(t *testing.T) {
	t.Parallel()
	for _, c := range [][2]string{{"runs", "parts"}, {"parts", "products"}, {"produces", "products"}, {"products", "protocol"}, {"products", "readiness"}} {
		if !jsonbBefore(c[0], c[1]) || jsonbBefore(c[1], c[0]) {
			t.Errorf("%s before %s", c[0], c[1])
		}
	}
	if jsonbBefore("products", "products") {
		t.Error("a key before itself")
	}
	for at, want := range map[time.Time]string{
		day:                               "2026-11-01T00:00:00+00:00",
		day.Add(1500 * time.Microsecond):  "2026-11-01T00:00:00.001500+00:00",
		day.Add(999 * time.Nanosecond):    "2026-11-01T00:00:00+00:00",
		day.In(time.FixedZone("x", 3600)): "2026-11-01T00:00:00+00:00",
	} {
		if got := pyISO(at); got != want {
			t.Errorf("%v: %s", at, got)
		}
	}
}
