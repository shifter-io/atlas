package build

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/shifter-io/atlas/api/internal/snap"
)

// A 24h fleet entry's IP slots as the calculation stores them: 24 UTC hours,
// the first before the data starts (null), the last the current hour (its
// start ten minutes before the as-of time) (FLEET-POOL-SERIES-1, #1302).
func slotsSnapshot(t *testing.T) *snap.Snapshot {
	t.Helper()
	return slotsOf(t, "24h", time.Date(2026, 11, 16, 5, 0, 0, 0, time.UTC), time.Hour, 24, "2026-11-17T04:10:00+00:00")
}

// slotsOf is a snapshot at asOf whose window's fleet entry has n slots of
// step from first, the first null, the others 1000 IPs.
func slotsOf(t *testing.T, window string, first time.Time, step time.Duration, n int, asOf string) *snap.Snapshot {
	t.Helper()
	slots := []string{}
	for h := range n {
		ips := "1000"
		if h == 0 {
			ips = "null"
		}
		at := first.Add(time.Duration(h) * step).Format(time.RFC3339)
		slots = append(slots, fmt.Sprintf(`{"at":%q,"unique_ips":%s}`, at, ips))
	}
	row := `{"cohort":"*","window":"` + window + `","unique_ips":5000,"series":[],"ip_slots":[` + strings.Join(slots, ",") + `]}`
	var f snap.FleetRow
	if err := json.Unmarshal([]byte(row), &f); err != nil {
		t.Fatal(err)
	}
	return &snap.Snapshot{AsOf: asOf, Fleet: []snap.FleetRow{f}}
}

func TestUniqueIpsCurveIsTheFleetsSlots(t *testing.T) {
	t.Parallel()
	c := uniqueIpsCurve(slotsSnapshot(t), "24h")
	if len(c.Values) != 24 || len(c.Times) != 24 {
		t.Fatalf("%d values, %d times", len(c.Values), len(c.Times))
	}
	if c.Values[0].OK() || c.Values[1] != 1000 || c.Values[23] != 1000 {
		t.Errorf("values %v", c.Values)
	}
	// Each slot's start in minutes before the as-of time: 05:00 the day
	// before is 23 h 10 min back, the current hour's 04:00 ten minutes.
	if c.Times[0] != 1390 || c.Times[1] != 1330 || c.Times[23] != 10 {
		t.Errorf("times %v", c.Times)
	}
	body, err := json.Marshal(c)
	if err != nil || !strings.HasPrefix(string(body), `{"values":[null,1000,`) {
		t.Errorf("%s, %v", body, err)
	}
}

// No snapshot, no fleet entry of the range or a build before the slots: an
// empty curve (lists, never null).
func TestUniqueIpsCurveWithoutSlotsIsEmpty(t *testing.T) {
	t.Parallel()
	old := &snap.Snapshot{AsOf: "2026-11-17T04:10:00+00:00", Fleet: []snap.FleetRow{{Cohort: snap.AllCohorts, Window: "7d"}}}
	for name, c := range map[string]UniqueIpsSeries{"nil": uniqueIpsCurve(nil, "24h"), "other range": uniqueIpsCurve(slotsSnapshot(t), "7d"),
		"before the slots": uniqueIpsCurve(old, "7d")} {
		body, _ := json.Marshal(c)
		if string(body) != `{"values":[],"times":[],"partialLast":false}` {
			t.Errorf("%s: %s", name, body)
		}
	}
}

// The home market serves it beside uniqueIps.
func TestHomeMarketServesUniqueIpsCurve(t *testing.T) {
	t.Parallel()
	m := HomeMarket{UniqueIpsCurve: uniqueIpsCurve(slotsSnapshot(t), "24h")}
	body, err := json.Marshal(m)
	if err != nil || !strings.Contains(string(body), `"uniqueIpsCurve":{"values":[null,1000,`) {
		t.Errorf("%s, %v", body, err)
	}
}

// FLEET-POOL-SERIES-2: the last point of a daily curve is flagged while its
// day is unfinished (today 500 minutes in, or an empty day starting at the
// as-of time) and not after a whole day (a midnight build, whose last slot is
// the day just ended, series_axis()); a 24h point always counts a whole 24
// hours (rolling, FLEET-POOL-SERIES-3), so never there.
func TestUniqueIpsCurveFlagsTheUnfinishedLastPoint(t *testing.T) {
	t.Parallel()
	day := 24 * time.Hour
	week := slotsOf(t, "7d", time.Date(2026, 11, 11, 0, 0, 0, 0, time.UTC), day, 7, "2026-11-17T08:20:00+00:00")
	midnight := slotsOf(t, "7d", time.Date(2026, 11, 12, 0, 0, 0, 0, time.UTC), day, 7, "2026-11-18T00:00:00+00:00")
	whole := slotsOf(t, "7d", time.Date(2026, 11, 11, 0, 0, 0, 0, time.UTC), day, 7, "2026-11-18T00:00:00+00:00")
	for name, c := range map[string]struct {
		curve   UniqueIpsSeries
		last    Num
		partial bool
	}{
		"24h, rolling": {uniqueIpsCurve(slotsSnapshot(t), "24h"), 10, false},
		"today":        {uniqueIpsCurve(week, "7d"), 500, true},
		"at midnight":  {uniqueIpsCurve(midnight, "7d"), 0, true},
		"whole day":    {uniqueIpsCurve(whole, "7d"), 1440, false},
	} {
		times := c.curve.Times
		if len(times) == 0 || times[len(times)-1] != c.last || c.curve.PartialLast != c.partial {
			t.Errorf("%s: times %v, partialLast %v", name, times, c.curve.PartialLast)
		}
	}
	body, _ := json.Marshal(uniqueIpsCurve(week, "7d"))
	if !strings.HasSuffix(string(body), `"times":[9140,7700,6260,4820,3380,1940,500],"partialLast":true}`) {
		t.Errorf("%s", body)
	}
}
