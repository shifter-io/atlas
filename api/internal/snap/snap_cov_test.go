package snap

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

// A snapshot that sets at least one field of every decoded type and leaves
// others out, so a missing key is seen to decode as NaN.
const covSnapshot = `{
 "schema_version": 4, "snapshot_id": "s9", "as_of": "2026-09-26T14:25:00+00:00",
 "readiness": "ready",
 "methodology": {"id": "m1", "settings": {"weights": {"speed": 14}, "target_groups": {"ai_answers": ["chat_a", "chat_b"]}}},
 "products": [{
  "id": "1:a", "product_id": 7, "provider": "acme", "kind": "isp_static",
  "criteria": {"country": null, "requested_countries": ["DE", "US"], "groups": 3},
  "census": [{"country": "US", "requests": 3000, "unique_ips": 2980}],
  "pricing": {"currency": "USD", "lowest_per_gb": 0.75, "sources": ["s"]},
  "outages": [{"country": "US", "source": "pulse", "started_at": "2026-09-26T08:00:00Z", "ended_at": null}],
  "windows": {"30d": {
   "period": {"start": "a", "hours": 720},
   "readiness": "ready",
   "metrics": {"completed": 5000, "latency_ms": {"p50": 1024},
    "under_load": {"slots": 3, "reason": "few"},
    "time_slots": {"minutes": 5, "latency_targets": ["generic_http"]}},
   "pool": {"unique_ips": 42, "shares_basis": "ips",
    "countries_breakdown": [{"country": "Germany", "country_code": "DE", "ips": 30}],
    "asns_breakdown": [{"asn": 64500, "name": "Doc", "ips": 12}]},
   "targets": {
    "generic_http": {"metrics": {"completed": 10}},
    "ai_answers": {"metrics": {"completed": 4}, "browser": {"outcome_rates": {"ok": 0.75, "block": 0.25},
     "page_load_ms": {"p50": 2100}, "sources": ["chat_a", "chat_b"]}}},
   "scores": {"balanced": {"score": 88, "grade": "A", "state": "ready", "components": {
    "speed": {"value": 100, "state": "ready", "inputs": {"peers": 3}},
    "value": {"value": 90, "state": "ready", "inputs": {"peers": 5}},
    "uptime": {"value": 80, "state": "ready", "inputs": 4},
    "coverage": {"state": "unavailable"}}}},
   "series": [{"at": "2026-09-26T14:20:00Z", "completed": 9, "latency_under_load_ms": {"p99": 3000}}],
   "success_delta": 0.01,
   "overlap": {"exclusive_share": 0.6, "peers": [{"provider": "other", "product_id": 8, "shared_ips": 5}]},
   "uptime": {"batches": 1440, "basis": "outage time"},
   "breakdowns": {"city": [{"country_code": "DE", "city": "Berlin", "ips": 7}]},
   "breakdown_entries": {"city": 120},
   "estimated": {"unique_ips": {"value": 60}, "series": [{"start": "2026-09-26T13:00:00Z", "unique_ips": 50}]}
  }}
 }],
 "fleet": [
  {"cohort": "c1", "window": "24h", "products": 1},
  {"cohort": "*", "window": "24h", "providers": 2, "unique_ips": 99, "series": [{"at": "x", "uptime": 1}]}
 ],
 "overlap": [{"cohort": "c1", "window": "30d", "product_a": 7, "product_b": 8, "shared_ips": 5}]
}`

func nan(n Num) bool { return math.IsNaN(float64(n)) }

func TestDecodeEveryType(t *testing.T) {
	var s Snapshot
	if err := json.Unmarshal([]byte(covSnapshot), &s); err != nil {
		t.Fatal(err)
	}
	if s.SchemaVersion != 4 || !nan(s.RefreshSeconds) || s.ImplementationID != nil || s.Methodology.ID != "m1" {
		t.Fatalf("head %+v", s)
	}
	if g := s.Methodology.Settings.TargetGroups["ai_answers"]; !reflect.DeepEqual(g, []string{"chat_a", "chat_b"}) {
		t.Fatalf("target groups %v", g)
	}
	p := &s.Products[0]
	if p.ProductID != 7 || p.Criteria.Country != nil || p.Criteria.Groups != 3 || len(p.Criteria.RequestedCountries) != 2 {
		t.Fatalf("product %+v", p)
	}
	var raw map[string]any
	if err := json.Unmarshal(p.Raw, &raw); err != nil || raw["provider"] != "acme" {
		t.Fatalf("raw product %s %v", p.Raw, err)
	}
	if c := p.Census[0]; c.Requests != 3000 || !nan(c.Successes) || c.Region != nil {
		t.Fatalf("census %+v", c)
	}
	if pr := p.Pricing; pr.LowestPerGb != 0.75 || !nan(pr.EntryPerGb) || *pr.Currency != "USD" {
		t.Fatalf("pricing %+v", pr)
	}
	if o := p.Outages[0]; o.EndedAt != nil || !nan(o.DurationS) || o.Source != "pulse" {
		t.Fatalf("outage %+v", o)
	}
	w := p.Window("30d")
	if w.Period.Hours != 720 || w.Period.End != "" || w.SuccessDelta != 0.01 {
		t.Fatalf("window %+v", w.Period)
	}
	m := w.Metrics
	if !m.EveryRequest() || m.Completed != 5000 || !nan(m.Successes) || m.LatencyMs.P50 != 1024 || !nan(m.LatencyMs.P99) {
		t.Fatalf("metrics %+v", m)
	}
	if m.UnderLoad.Slots != 3 || *m.UnderLoad.Reason != "few" {
		t.Fatalf("under load %+v", m.UnderLoad)
	}
	if m.TimeSlots.Minutes != 5 || !nan(m.TimeSlots.Success) || m.TimeSlots.LatencyTargets[0] != "generic_http" {
		t.Fatalf("time slots %+v", m.TimeSlots)
	}
	if m.LatencyUnderLoadMs != nil {
		t.Fatal("latency under load without the key")
	}
	pool := w.Pool
	if pool.UniqueIps != 42 || !nan(pool.ChurnRate) || pool.CountriesBreakdown[0].Ips != 30 || pool.AsnsBreakdown[0].Asn != 64500 || pool.AsnsBreakdown[0].UsageType != "" {
		t.Fatalf("pool %+v", pool)
	}
	if tr := w.Targets["generic_http"]; tr.Browser != nil || tr.Metrics.Completed != 10 {
		t.Fatalf("network target %+v", tr)
	}
	br := w.Targets["ai_answers"].Browser
	if br == nil || br.OutcomeRates.Ok != 0.75 || !nan(br.OutcomeRates.Timeout) || br.PageLoadMs.P50 != 2100 || br.Group != nil || len(br.Sources) != 2 {
		t.Fatalf("browser target %+v", br)
	}
	sr := w.Series[0]
	if sr.Completed != 9 || !nan(sr.Successes) || sr.LatencyUnderLoadMs.P99 != 3000 || !nan(sr.LatencyUnderLoadMs.P50) {
		t.Fatalf("series %+v", sr)
	}
	if ov := w.Overlap; ov.ExclusiveShare != 0.6 || !nan(ov.SharedShare) || ov.Peers[0].SharedIps != 5 || !nan(ov.Peers[0].IpsOurs) {
		t.Fatalf("overlap %+v", ov)
	}
	if u := w.Uptime; u.Batches != 1440 || !nan(u.Up) || *u.Basis != "outage time" {
		t.Fatalf("uptime %+v", u)
	}
	if b := w.Breakdowns["city"][0]; *b.City != "Berlin" || b.Ips != 7 || !nan(b.Asn) || b.Region != nil || w.BreakdownEntries["city"] != 120 {
		t.Fatalf("breakdown %+v", b)
	}
	if e := w.Estimated; e.UniqueIps.Value != 60 || e.Series[0].UniqueIps != 50 || !nan(e.BreakdownScale) {
		t.Fatalf("estimated %+v", e)
	}
	sc := w.Scores["balanced"]
	if sc.Score != 88 || !nan(sc.Rank) || *sc.Grade != "A" || sc.Components["coverage"].State != "unavailable" || !nan(sc.Components["coverage"].Value) {
		t.Fatalf("score %+v", sc)
	}
	if o := s.Overlap[0]; o.SharedIps != 5 || !nan(o.IpsA) || o.Window != "30d" {
		t.Fatalf("overlap row %+v", o)
	}
}

func TestPeers(t *testing.T) {
	var s Snapshot
	if err := json.Unmarshal([]byte(covSnapshot), &s); err != nil {
		t.Fatal(err)
	}
	sc := s.Products[0].Window("30d").Scores["balanced"]
	// The most peers of any component; a bare number or no inputs has none.
	if got := sc.Peers(); got != 5 {
		t.Fatalf("score peers %v, want 5", got)
	}
	for _, k := range []string{"uptime", "coverage"} {
		if got := sc.Components[k].Peers(); !nan(got) {
			t.Errorf("%s peers %v, want NaN", k, got)
		}
	}
	if got := (Component{Inputs: json.RawMessage(`{"peers":null}`)}).Peers(); !nan(got) {
		t.Errorf("null peers %v", got)
	}
	if got := EmptyScore().Peers(); !nan(got) {
		t.Errorf("empty score peers %v", got)
	}
	if e := EmptyScore(); !nan(e.Score) || !nan(e.Rank) || !nan(e.PreviousRank) || e.Components != nil {
		t.Errorf("empty score %+v", e)
	}
}

func TestFleetAll(t *testing.T) {
	var s Snapshot
	if err := json.Unmarshal([]byte(covSnapshot), &s); err != nil {
		t.Fatal(err)
	}
	f := s.FleetAll("24h")
	if f == nil || f.Cohort != AllCohorts || f.Providers != 2 || !nan(f.Products) || f.Series[0].Uptime != 1 || f != &s.Fleet[1] {
		t.Fatalf("FleetAll(24h) = %+v", f)
	}
	if s.FleetAll("30d") != nil {
		t.Fatal("FleetAll of a window without a '*' entry")
	}
	if c := s.Fleet[0]; c.Products != 1 || !nan(c.UniqueIps) || !nan(c.Metrics.Completed) {
		t.Fatalf("cohort fleet row %+v", c)
	}
}

// Window falls back to an empty window: every number NaN.
func TestWindowFallback(t *testing.T) {
	p := &Product{}
	w := p.Window("7d")
	if !nan(w.Metrics.Completed) || !nan(w.Pool.UniqueIps) || !nan(w.Period.Hours) || !nan(w.SuccessDelta) || !nan(w.Overlap.SharedShare) {
		t.Fatalf("empty window %+v", w)
	}
	if w.Metrics.EveryRequest() || w.Targets != nil || w.Uptime != nil {
		t.Fatalf("empty window has content %+v", w)
	}
}

func TestProxyKindAndByKind(t *testing.T) {
	for k, want := range map[string]string{"isp": "isp", "mobile": "mobile", "datacenter": "datacenter", "residential": "residential", "isp_static": "residential", "": "residential"} {
		if got := ProxyKind(k); got != want {
			t.Errorf("ProxyKind(%q) = %q, want %q", k, got, want)
		}
	}
	mk := func(id, kind string, completed float64) *Product {
		w := EmptyWindow()
		w.Metrics.Completed = Num(completed)
		return &Product{ID: id, Kind: kind, Windows: map[string]Window{"30d": w}}
	}
	a, b, c, d, e := mk("a", "isp", 10), mk("b", "datacenter", 5), mk("c", "isp", 20), mk("d", "isp", 20), mk("e", "other", 1)
	noWin := &Product{ID: "f", Kind: "datacenter"}
	byKind, kinds := ByKind([]*Product{a, b, c, d, e, noWin})
	if !reflect.DeepEqual(kinds, []string{"isp", "datacenter", "residential"}) {
		t.Fatalf("kinds %v", kinds)
	}
	// The most completed requests wins, the first on a tie; NaN never beats a number.
	if byKind["isp"] != c || byKind["datacenter"] != b || byKind["residential"] != e {
		t.Fatalf("byKind isp=%s datacenter=%s residential=%s", byKind["isp"].ID, byKind["datacenter"].ID, byKind["residential"].ID)
	}
	if byKind, kinds := ByKind(nil); len(byKind) != 0 || len(kinds) != 0 {
		t.Fatal("ByKind(nil)")
	}
}

func TestParseTimeAndMs(t *testing.T) {
	want := time.Date(2026, 9, 26, 14, 25, 0, 500_000_000, time.UTC)
	for _, s := range []string{
		"2026-09-26T14:25:00.5Z",
		"2026-09-26T14:25:00.5+00:00",
		"2026-09-26T14:25:00.500000+0000",
		"2026-09-26T14:25:00.5",
		"2026-09-26 14:25:00.5+00:00",
	} {
		got, ok := ParseTime(s)
		if !ok || !got.Equal(want) {
			t.Errorf("ParseTime(%q) = %v %v, want %v", s, got, ok, want)
		}
		if ms := Ms(s); ms != float64(want.UnixMilli()) {
			t.Errorf("Ms(%q) = %v", s, ms)
		}
	}
	for _, s := range []string{"", "yesterday", "2026-09-26"} {
		if got, ok := ParseTime(s); ok || !got.IsZero() {
			t.Errorf("ParseTime(%q) = %v %v, want unparseable", s, got, ok)
		}
		if !math.IsNaN(Ms(s)) {
			t.Errorf("Ms(%q) is not NaN", s)
		}
	}
}

// Every decoder reports a wrongly typed field instead of guessing.
func TestDecodeErrors(t *testing.T) {
	for _, v := range []any{
		&Latency{}, &Metrics{}, &UnderLoad{}, &TimeSlots{}, &CountryIPs{}, &AsnIPs{}, &Pool{}, &Pricing{},
		&Product{}, &Criteria{}, &Snapshot{}, &Census{}, &Outage{}, &OverlapRow{}, &FleetRow{},
		&Component{}, &Score{}, &BrowserResult{}, &OutcomeRates{}, &TargetResult{}, &SeriesRow{}, &Period{},
		&Uptime{}, &BreakdownRow{}, &OverlapPeer{}, &WindowOverlap{}, &Window{}, &Estimated{}, &EstimatedValue{}, &EstimatedHour{},
	} {
		if err := json.Unmarshal([]byte(`[1]`), v); err == nil {
			t.Errorf("%T: an array decoded", v)
		}
	}
	var p Product
	if err := json.Unmarshal([]byte(`{"id":"x","product_id":"seven"}`), &p); err == nil || p.Raw != nil {
		t.Fatalf("a bad product decoded: %v raw %s", err, p.Raw)
	}
}

// The committed every-request fixture decodes with its figures in place.
func TestDecodeEveryRequestFixture(t *testing.T) {
	b, err := os.ReadFile(filepath.Join("..", "..", "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil {
		t.Fatal(err)
	}
	var s Snapshot
	if err := json.Unmarshal(b, &s); err != nil {
		t.Fatal(err)
	}
	if s.SchemaVersion != 2 || s.SnapshotID != "3" || s.RefreshSeconds != 60 || len(s.Products) != 1 {
		t.Fatalf("head %+v", s)
	}
	p := s.Products[0]
	w := p.Window("30d")
	if p.Provider != "shifter" || !w.Metrics.EveryRequest() || w.Metrics.Completed != 5000 || w.Metrics.LatencyMs.P50 != 1024 {
		t.Fatalf("product %s metrics %+v", p.Provider, w.Metrics)
	}
	if w.Uptime == nil || w.Uptime.Batches != 1440 || p.Pricing.EntryPerGb != 1 || p.Outages[0].DurationS != 7200 {
		t.Fatalf("uptime %+v pricing %+v outages %+v", w.Uptime, p.Pricing, p.Outages)
	}
	if !nan(w.Overlap.SharedShare) || w.Overlap.ComparedProducts != 0 {
		t.Fatalf("overlap %+v", w.Overlap)
	}
	if s.FleetAll("24h") != nil || len(s.Fleet) != 3 {
		t.Fatalf("fleet %+v", s.Fleet)
	}
}
