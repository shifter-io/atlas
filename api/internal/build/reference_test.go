package build_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/ice-zeus/proxy-index-website/api/internal/build"
	"github.com/ice-zeus/proxy-index-website/api/internal/config"
	"github.com/ice-zeus/proxy-index-website/api/internal/jsnum"
	"github.com/ice-zeus/proxy-index-website/api/internal/model"
	"github.com/ice-zeus/proxy-index-website/api/internal/store"
)

// Documented deviations of the Go port from presentation.ts for a measured
// provider (FIELDS.md, "Deviations from the TypeScript"). Paths are relative
// to the provider or to one of its productMeasurements entries.
var deviations = []*regexp.Regexp{
	// Placeholder copy for data nobody collected is null (N/A) instead.
	regexp.MustCompile(`^\.(hq|claim)$`),
	regexp.MustCompile(`^\.pricing\.(model|trial|costBasis)$`),
	regexp.MustCompile(`^\.trust\.(stickinessAdvertised|kycVerified|consentDisclosed)$`),
	regexp.MustCompile(`^\.products\[0\]\.(url|priceLabel|note)$`),
	regexp.MustCompile(`^\.score\.grade$`),
	// presentation.ts divides milliseconds by 60; the API publishes minutes.
	regexp.MustCompile(`^\.series\.\w+\[\d+\]\.t$`),
	// presentation.ts spreads the measurement into the provider, leaking its
	// kind/product; the Provider contract has primaryKind/measuredProduct.
	regexp.MustCompile(`^\.(kind|product)$`),
	// The snapshot's 30d overlap shares are mapped instead of left unavailable.
	regexp.MustCompile(`^\.overlap\.(exclusiveShare|sharedShare)$`),
}

// JavaScript's ** is the platform libm's pow (macOS libm, glibc), so values
// derived from it agree with Go's math.Pow only to the last bit or two, and
// differ between platforms for the site itself.
var powDerived = regexp.MustCompile(`\.churnRate: want ([^,]+), got (.+)$`)

func closeEnough(d string) bool {
	m := powDerived.FindStringSubmatch(d)
	if m == nil {
		return false
	}
	var a, b float64
	if _, err := fmt.Sscan(m[1], &a); err != nil {
		return false
	}
	if _, err := fmt.Sscan(m[2], &b); err != nil {
		return false
	}
	return math.Abs(a-b) <= 1e-14*math.Abs(a)
}

var providerPath = regexp.MustCompile(`^\.providers\[(\d+)\](\.productMeasurements\.\w+)?(\..*)$`)

func TestPortMatchesTheSiteTypeScript(t *testing.T) {
	snapshot, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot.json"))
	if err != nil {
		t.Fatal(err)
	}
	comparePort(t, snapshot, "")
}

// The committed fixture is a v1 snapshot without outages; this runs the same
// comparison over it turned into v2 with outages and time-based uptime.
func TestPortMatchesTheSiteTypeScriptWithOutages(t *testing.T) {
	snapshot := withOutages(t)
	file := filepath.Join(t.TempDir(), "snapshot.json")
	if err := os.WriteFile(file, snapshot, 0o600); err != nil {
		t.Fatal(err)
	}
	res := comparePort(t, snapshot, file)
	p := res.Details["shifter"].Provider
	if len(p.Incidents) != len(fixtureOutages) {
		t.Fatalf("incidents %d, want %d", len(p.Incidents), len(fixtureOutages))
	}
	us := p.Incidents[1]
	if us.StartedAgoSeconds != 6*3600+25*60 || us.DurationMinutes != 120 || model.SV(us.Scope) != "US" || us.Impact != 0.5 {
		t.Errorf("2-hour US outage mapped as %+v", us)
	}
	if p.Performance.DowntimeMinutes24h != 75 { // 9000 s over 2 countries
		t.Errorf("downtimeMinutes24h %v", p.Performance.DowntimeMinutes24h)
	}
	for _, c := range []struct {
		rangeID  string
		count    int
		downtime float64
	}{{"24h", 2, 75}, {"7d", 2, 75}, {"30d", 3, 135}} {
		w := res.Details["shifter"].Windows["residential"][c.rangeID]
		if len(w.Availability) != len(w.Success) || w.Availability[len(w.Availability)-1].V != 0.9995 || w.Availability[0].V.OK() {
			t.Errorf("%s: availability history lost measured fractions or null gaps", c.rangeID)
		}
		if len(w.Bands) != c.count || w.IncidentCount != Num(c.count) || w.DowntimeMinutes != Num(c.downtime) {
			t.Errorf("%s: %d bands, count %v, downtime %v", c.rangeID, len(w.Bands), w.IncidentCount, w.DowntimeMinutes)
		}
	}
	// The US outage sits 6h25m to 4h25m before the end of the 24h window.
	b := res.Details["shifter"].Windows["residential"]["24h"].Bands[1]
	if math.Abs(b.From.F()-(1-385.0/1440)) > 1e-12 || math.Abs(b.To.F()-(1-265.0/1440)) > 1e-12 || b.Label != "major · US" {
		t.Errorf("24h band %+v", b)
	}
}

// The framework's every-request snapshot shape (fixtures/measured/
// snapshot-every-request.json, scripts/every-request-fixture.mjs): latency
// under load, time-weighted success, per-IP shares and list prices.
func TestPortMatchesTheSiteTypeScriptForEveryRequestSnapshots(t *testing.T) {
	file := repoFile(t, "fixtures", "measured", "snapshot-every-request.json")
	snapshot, err := os.ReadFile(file)
	if err != nil {
		t.Fatal(err)
	}
	res := comparePort(t, snapshot, file)
	p := res.Details["shifter"].Provider
	w := res.Details["shifter"].Windows["residential"]["24h"]
	if !w.LightLatencyP50.OK() || !(w.LatencyP50 > w.LightLatencyP50) {
		t.Errorf("headline %v should be the latency under load, above light load %v", w.LatencyP50, w.LightLatencyP50)
	}
	// Latency is the HTTP target's only, never the window's HTTP + HTTPS mixture.
	var snapshotDoc struct {
		Products []struct {
			Windows map[string]struct {
				Targets map[string]struct {
					Metrics struct {
						LatencyMs map[string]float64 `json:"latency_ms"`
					} `json:"metrics"`
				} `json:"targets"`
			} `json:"windows"`
		} `json:"products"`
	}
	if err := json.Unmarshal(snapshot, &snapshotDoc); err != nil {
		t.Fatal(err)
	}
	httpLight := snapshotDoc.Products[0].Windows["24h"].Targets["generic_http"].Metrics.LatencyMs["p50"]
	if w.LatencyTarget == nil || *w.LatencyTarget != "generic_http" || w.LightLatencyP50.F() != httpLight {
		t.Errorf("latency target %v, light p50 %v, want generic_http %v", w.LatencyTarget, w.LightLatencyP50, httpLight)
	}
	for _, b := range p.Targets {
		if b.Target != "generic_http" && b.LatencyP50.OK() {
			t.Errorf("%s shows a latency; only generic_http may", b.Target)
		}
	}
	// Snapshot 69 on, the window's latency is the latency target's alone, so the
	// series is charted.
	charted := false
	for _, pt := range w.Latency {
		charted = charted || pt.P50.OK()
	}
	if !charted {
		t.Errorf("the HTTP-only window latency series should be charted")
	}
	if !strings.Contains(model.SV(p.Verdict), "alone in its kind") || !strings.Contains(model.SV(p.Tagline), "census, pulse and load") {
		t.Errorf("copy does not follow the data: %q / %q", model.SV(p.Verdict), model.SV(p.Tagline))
	}
	if got := p.Score.KindStanding(); got != "#1 of 1 in its kind" {
		t.Errorf("a ready score alone in its kind reads %q", got)
	}
	for _, x := range p.Weaknesses {
		if strings.Contains(x, "Pricing") {
			t.Errorf("a priced network says pricing is missing: %q", x)
		}
	}
	if p.Pricing.PricePerGbAtScale != 0.75 || p.Pricing.PricePerGb != 1 || model.SV(p.Products[0].PriceLabel) != "$0.75–$1.00/GB" {
		t.Errorf("pricing %+v, label %v", p.Pricing, model.SV(p.Products[0].PriceLabel))
	}
	if !p.Pricing.CostPer1kSuccess.OK() || !w.CostPer1kSuccess.OK() {
		t.Errorf("a priced measured network needs a cost: %v / %v", p.Pricing.CostPer1kSuccess, w.CostPer1kSuccess)
	}
	if p.Trust.ResidentialAuthenticity != 0.987 || p.Trust.ClaimGap.OK() {
		t.Errorf("trust %+v", p.Trust)
	}
	for _, b := range res.Overview.CostBoards {
		if b.Ranks["shifter"] == 0 {
			t.Errorf("%s cost board leaves the priced measured network out: %+v", b.Range, b.Ranks)
		}
	}
}

// fixtureOutages: a 2-hour pulse outage in the US that ended 4h25m before
// as_of, one still open in DE for the last 30 minutes, and a 2-hour one ten
// days back (only in the 30d window). Two countries are tested.
var fixtureOutages = []map[string]any{
	{"country": "DE", "region": nil, "source": "census", "started_at": "2026-09-26T13:55:00+00:00", "ended_at": nil, "duration_s": 1800.0},
	{"country": "US", "region": "nyc1", "source": "pulse", "started_at": "2026-09-26T08:00:00+00:00", "ended_at": "2026-09-26T10:00:00+00:00", "duration_s": 7200.0},
	{"country": "DE", "region": "fra1", "source": "pulse", "started_at": "2026-09-16T12:00:00+00:00", "ended_at": "2026-09-16T14:00:00+00:00", "duration_s": 7200.0},
}

// withOutages is the committed fixture snapshot as v2 with fixtureOutages and
// the matching time-based uptime per window.
func withOutages(t *testing.T) []byte {
	t.Helper()
	body, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot.json"))
	if err != nil {
		t.Fatal(err)
	}
	var s map[string]any
	if err := json.Unmarshal(body, &s); err != nil {
		t.Fatal(err)
	}
	s["schema_version"] = 2
	product := s["products"].([]any)[0].(map[string]any)
	product["outages"] = fixtureOutages
	downtime := map[string]float64{"24h": 9000, "7d": 9000, "30d": 16200}
	for label, w := range product["windows"].(map[string]any) {
		w := w.(map[string]any)
		hours := w["period"].(map[string]any)["hours"].(float64)
		value := 1 - downtime[label]/(hours*3600*2)
		w["metrics"].(map[string]any)["uptime"] = value
		w["uptime"] = map[string]any{"batches": 48, "up": 47, "value": value, "downtime_s": downtime[label], "basis": "outage time", "countries": 2}
		points := w["series"].([]any)
		for i, point := range points {
			point := point.(map[string]any)
			point["uptime"] = nil
			if i == len(points)-1 {
				point["uptime"] = 0.9995
			}
		}
	}
	out, err := json.Marshal(s)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// comparePort publishes snapshot over the seeded demo data and compares the
// build with what the site's TypeScript computes from the same inputs
// (snapshotFile, when set, replaces the committed fixture for the TypeScript).
func comparePort(t *testing.T, snapshot []byte, snapshotFile string) *build.Result {
	t.Helper()
	node, err := exec.LookPath("node")
	script := config.FindUp(filepath.Join("api", "scripts", "reference.mjs"))
	esbuild := config.FindUp(filepath.Join("node_modules", "esbuild", "lib", "main.js"))
	if err != nil || script == "" || esbuild == "" {
		t.Skip("needs node and the site's node_modules (esbuild) to run the TypeScript reference")
	}
	db := seeded(t)
	db.Publish(t, snapshot)
	res := buildFrom(t, db)

	cases := tapeCases()
	casesFile := filepath.Join(t.TempDir(), "tape.json")
	tsProducts := []map[string]string{}
	for _, p := range cases.products {
		tsProducts = append(tsProducts, map[string]string{"provider": p.Provider, "name": p.Name, "kind": p.Kind})
	}
	caseBody, _ := json.Marshal(map[string]any{"events": cases.events, "products": tsProducts, "generatedAt": cases.generatedAt})
	if err := os.WriteFile(casesFile, caseBody, 0o600); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(node, script, casesFile)
	if snapshotFile != "" {
		cmd.Env = append(os.Environ(), "REFERENCE_SNAPSHOT="+snapshotFile)
	}
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("reference script: %v", err)
	}
	var ref map[string]json.RawMessage
	if err := json.Unmarshal(out, &ref); err != nil {
		t.Fatal(err)
	}
	measuredIndex := -1
	for i, p := range res.Dataset.Providers {
		if p.Source == "measured" {
			measuredIndex = i
		}
	}
	allowed := func(path string) bool {
		// Measured-only insight totals are an API addition, checked separately.
		if path == ".measuredTotals" || path == ".measuredMarket" {
			return true
		}
		m := providerPath.FindStringSubmatch(path)
		if m == nil || m[1] != itoa(measuredIndex) {
			return false
		}
		for _, d := range deviations {
			if d.MatchString(m[3]) {
				return true
			}
		}
		return false
	}
	check := func(name string, expected []byte, actual any) {
		var diffs []string
		diff(generic(t, expected), generic(t, actual), "", &diffs, map[string]bool{})
		var real []string
		for _, d := range diffs {
			path, _, _ := strings.Cut(d, ":")
			if !allowed(path) && !closeEnough(d) {
				real = append(real, d)
			}
		}
		if len(real) > 0 {
			t.Errorf("%s differs from the TypeScript:\n%s", name, strings.Join(real, "\n"))
		}
	}
	check("dataset", ref["dataset"], res.Dataset)
	if res.Dataset.MeasuredTotals.Providers != 1 {
		t.Errorf("insight totals include demo providers: %+v", res.Dataset.MeasuredTotals)
	}
	measured := res.Dataset.Providers[measuredIndex]
	if res.Dataset.MeasuredTotals.MisclassifiedShare != measured.Trust.DatacenterAsnShare {
		t.Errorf("insight misclassification %v, measured provider %v", res.Dataset.MeasuredTotals.MisclassifiedShare, measured.Trust.DatacenterAsnShare)
	}

	var windows map[string]map[string]json.RawMessage
	if err := json.Unmarshal(ref["windows"], &windows); err != nil {
		t.Fatal(err)
	}
	for _, sum := range res.Overview.Leaderboard {
		for rangeID, w := range windows[sum.Slug] {
			check("windowStats("+sum.Slug+", "+rangeID+")", w, sum.Windows[rangeID])
		}
	}
	var fleet []json.RawMessage
	json.Unmarshal(ref["fleetWindows"], &fleet) //nolint:errcheck
	for i, f := range fleet {
		check("fleetWindows["+itoa(i)+"]", f, res.Overview.FleetViews[i].Fleet)
	}
	check("costBoards", ref["costBoards"], res.Overview.CostBoards)
	check("tapeFromEvents", ref["tape"], build.TapeFromEvents(cases.events, cases.products, cases.generatedAt, nil))
	check("fleetViews", ref["fleetViews"], res.Overview.FleetViews)
	if !bytes.Contains(ref["dataset"], []byte(`"shifter"`)) || measuredIndex < 0 {
		t.Fatal("reference has no measured provider; the comparison would be vacuous")
	}
	return res
}

type Num = jsnum.Num

func itoa(n int) string {
	b, _ := json.Marshal(n)
	return string(b)
}

type tapeCaseSet struct {
	events      []store.EventJSON
	products    []build.TapeProduct
	generatedAt string
}

// tapeCases covers every event kind tape.ts describes, with and without values.
func tapeCases() tapeCaseSet {
	str := func(s string) *string { return &s }
	data := []string{
		`{"requests": 5000, "duration_s": 312, "unique_ips": 4100}`, `{"requests": 50, "duration_s": 12}`, `{"stop_reason": "stalled", "requests": 900}`,
		`{"reason": "no node"}`, `{"requests": 100, "success_rate": 0.974, "p50": 412, "challenges": 1, "blocks": 3}`, `{"requests": 100}`,
		`{"success_rate": 0.42, "baseline": 0.95}`, `{"value": 0.5}`, `{}`, `{"p50": 1840, "baseline": 400}`, `{"value": 2500}`, `{}`,
		`{"blocks": 12, "baseline": 0.02}`, `{}`, `{"source": "census"}`, `{"source": "pulse"}`, `{}`, `{"name": "Hetzner"}`, `{"value": 0.31}`, `{}`, `{}`, `{}`, `{"region": "ams3"}`,
		`{"region": "fra1", "retry_of": 41, "reason": "no_success", "pulse_batch_id": 77}`, `{}`,
	}
	kinds := []string{"census_finished", "census_finished", "census_finished", "census_skipped", "pulse_finished", "pulse_finished",
		"success_drop", "success_drop", "success_drop", "latency_spike", "latency_spike", "latency_spike",
		"block_spike", "block_spike", "down", "down", "recovered", "new_hosting_asn", "ip_overlap", "ip_overlap", "node_offline", "something_new", "no_node",
		"census_retry", "census_retry"}
	set := tapeCaseSet{generatedAt: "2026-10-01T12:00:05.123456+00:00", products: []build.TapeProduct{
		{Provider: "acme", Name: "Acme", Kind: "residential"}, {Provider: "zeta", Name: "Zeta", Kind: "mobile"}, {Provider: "acme", Name: "Acme Two", Kind: "isp"}}}
	set.events = append(set.events, store.EventJSON{ID: 100, At: "2026-10-01T11:00:00.000000+00:00", Kind: "census_started", Category: "census",
		Product: "acme/residential", Provider: "acme", Country: str("DE"), Region: str("fra1"), Target: str("generic_http"), Severity: "info", Data: json.RawMessage(`{}`)})
	for i, k := range kinds {
		e := store.EventJSON{ID: int64(i + 1), At: "2026-10-01T11:" + fmt.Sprintf("%02d", 10+i) + ":00.000000+00:00", Kind: k, Category: store.Category(k),
			Product: "zeta/mobile", Provider: "zeta", Signal: int16(i%3 - 1), Severity: "info", Data: json.RawMessage(data[i])}
		switch i % 4 {
		case 0:
			e.Country = str("US")
		case 1:
			e.Region = str("nyc1")
		case 2:
			e.Product, e.Provider = "", ""
		}
		set.events = append(set.events, e)
	}
	// Newest first, as the API and the snapshot list them.
	for i, j := 0, len(set.events)-1; i < j; i, j = i+1, j-1 {
		set.events[i], set.events[j] = set.events[j], set.events[i]
	}
	return set
}
