package build

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// A 24h fleet entry's IP slots as the calculation stores them: 24 UTC hours,
// the first before the data starts (null), the last the current hour,
// partial, ten minutes before the as-of time (FLEET-POOL-SERIES-1, #1302).
func slotsSnapshot(t *testing.T) *snap.Snapshot {
	t.Helper()
	first, slots := time.Date(2026, 11, 16, 5, 0, 0, 0, time.UTC), []string{}
	for h := range 24 {
		ips := "1000"
		if h == 0 {
			ips = "null"
		}
		at := first.Add(time.Duration(h) * time.Hour).Format(time.RFC3339)
		slots = append(slots, fmt.Sprintf(`{"at":%q,"unique_ips":%s}`, at, ips))
	}
	row := `{"cohort":"*","window":"24h","unique_ips":5000,"series":[],"ip_slots":[` + strings.Join(slots, ",") + `]}`
	var f snap.FleetRow
	if err := json.Unmarshal([]byte(row), &f); err != nil {
		t.Fatal(err)
	}
	return &snap.Snapshot{AsOf: "2026-11-17T04:10:00+00:00", Fleet: []snap.FleetRow{f}}
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
	for name, c := range map[string]SparkSeries{"nil": uniqueIpsCurve(nil, "24h"), "other range": uniqueIpsCurve(slotsSnapshot(t), "7d"),
		"before the slots": uniqueIpsCurve(old, "7d")} {
		body, _ := json.Marshal(c)
		if string(body) != `{"values":[],"times":[]}` {
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
