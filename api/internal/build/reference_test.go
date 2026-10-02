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

	"github.com/ice-zeus/milf-website/api/internal/build"
	"github.com/ice-zeus/milf-website/api/internal/config"
	"github.com/ice-zeus/milf-website/api/internal/store"
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
	node, err := exec.LookPath("node")
	script := config.FindUp(filepath.Join("api", "scripts", "reference.mjs"))
	esbuild := config.FindUp(filepath.Join("node_modules", "esbuild", "lib", "main.js"))
	if err != nil || script == "" || esbuild == "" {
		t.Skip("needs node and the site's node_modules (esbuild) to run the TypeScript reference")
	}
	db := seeded(t)
	snapshot, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot.json"))
	if err != nil {
		t.Fatal(err)
	}
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
	out, err := exec.Command(node, script, casesFile).Output()
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
}

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
	}
	kinds := []string{"census_finished", "census_finished", "census_finished", "census_skipped", "pulse_finished", "pulse_finished",
		"success_drop", "success_drop", "success_drop", "latency_spike", "latency_spike", "latency_spike",
		"block_spike", "block_spike", "down", "down", "recovered", "new_hosting_asn", "ip_overlap", "ip_overlap", "node_offline", "something_new", "no_node"}
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
