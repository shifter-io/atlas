package model

import (
	"bytes"
	"encoding/json"
	"math"
	"testing"
)

func TestScalarHelpers(t *testing.T) {
	if s := S("x"); s == nil || *s != "x" {
		t.Fatalf("S: %v", s)
	}
	if b := B(true); b == nil || !*b {
		t.Fatalf("B: %v", b)
	}
	if SV(nil) != "" || SV(S("ab")) != "ab" {
		t.Fatal("SV: nil is \"\", a string is itself")
	}
	for _, k := range ProxyKinds {
		if !IsProxyKind(k) {
			t.Errorf("IsProxyKind(%q) is false", k)
		}
	}
	for _, k := range []string{"", "residential_rotating", "Residential"} {
		if IsProxyKind(k) {
			t.Errorf("IsProxyKind(%q) is true", k)
		}
	}
}

// Get and Set reach every component by its contract key; an unknown key
// reads NaN and writes nothing.
func TestBreakdownGetSetEveryKey(t *testing.T) {
	b := NewBreakdown()
	for _, k := range BreakdownKeys {
		if b.Get(k).OK() {
			t.Fatalf("new breakdown %s = %v, want NaN", k, b.Get(k))
		}
	}
	for i, k := range BreakdownKeys {
		b.Set(k, Num(i+1))
	}
	for i, k := range BreakdownKeys {
		if got := b.Get(k); got != Num(i+1) {
			t.Errorf("Get(%q) = %v, want %d", k, got, i+1)
		}
	}
	before := b
	b.Set("nope", 99)
	if b != before {
		t.Errorf("Set of an unknown key changed the breakdown: %+v", b)
	}
	if b.Get("nope").OK() {
		t.Error("Get of an unknown key is not NaN")
	}
	// Every key round-trips through JSON under its contract name.
	raw, err := json.Marshal(&b)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]float64
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	for i, k := range BreakdownKeys {
		if m[k] != float64(i+1) {
			t.Errorf("json %s = %v, want %d", k, m[k], i+1)
		}
	}
}

func TestBreakdownDecodesMissingAsNaN(t *testing.T) {
	var b Breakdown
	if err := json.Unmarshal([]byte(`{"speed":0.5,"value":null}`), &b); err != nil {
		t.Fatal(err)
	}
	if b.Speed != 0.5 || b.Value.OK() || b.Reliability.OK() {
		t.Fatalf("decoded %+v", b)
	}
	if err := json.Unmarshal([]byte(`{"speed":"fast"}`), &b); err == nil {
		t.Fatal("a string component decoded")
	}
}

func TestStandingRank(t *testing.T) {
	nan := Num(math.NaN())
	for _, c := range []struct {
		name                   string
		composite, rank, peers Num
		want                   Num
	}{
		{"alone and scored", 71, nan, 1, 1},
		{"alone without a score", nan, nan, 1, nan},
		{"unranked among peers", 71, nan, 4, nan},
		{"ranked among peers", 71, 2, 1234, 2},
		{"ranked, peers unknown", 71, 3, 0, 3},
	} {
		if got := StandingRank(c.rank, c.composite, c.peers); got != c.want && (got.OK() || c.want.OK()) {
			t.Errorf("%s: %v, want %v", c.name, got, c.want)
		}
	}
}

// EmptyMeasured has every number unavailable and every list empty (never
// null), as the contract writes them.
func TestEmptyMeasuredIsAllNullWithEmptyLists(t *testing.T) {
	m := EmptyMeasured()
	if m.Pool.UniqueIps24h.OK() || m.Performance.LatencyP99.OK() || m.Pricing.ValueRank.OK() || m.Trust.ClaimGap.OK() || m.Overlap.SharedShare.OK() {
		t.Fatalf("a number is set: %+v", m)
	}
	raw, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"targets", "ipSamples", "incidents", "geo", "asns"} {
		if l, ok := got[k].([]any); !ok || len(l) != 0 {
			t.Errorf("%s = %v, want []", k, got[k])
		}
	}
	series := got["series"].(map[string]any)
	for _, k := range []string{"latency24h", "success24h", "uniqueIps24h", "uniqueIps30d", "latency30d", "success30d", "pricePerGb90d"} {
		if l, ok := series[k].([]any); !ok || len(l) != 0 {
			t.Errorf("series.%s = %v, want []", k, series[k])
		}
	}
	if l, ok := got["pricing"].(map[string]any)["plans"].([]any); !ok || len(l) != 0 {
		t.Errorf("pricing.plans = %v", got["pricing"])
	}
	if l, ok := got["trust"].(map[string]any)["leakChecks"].([]any); !ok || len(l) != 0 {
		t.Errorf("trust.leakChecks = %v", got["trust"])
	}
	if l, ok := got["overlap"].(map[string]any)["overlaps"].([]any); !ok || len(l) != 0 {
		t.Errorf("overlap.overlaps = %v", got["overlap"])
	}
	if p := got["pool"].(map[string]any); p["uniqueIps24h"] != nil {
		t.Errorf("pool.uniqueIps24h = %v, want null", p["uniqueIps24h"])
	}
}

// Normalize keeps slices that are already set.
func TestNormalizeKeepsSetSlices(t *testing.T) {
	m := Measured{
		Targets: []TargetBenchmark{{Target: "a"}},
		Series:  ProviderSeries{Success24h: []SeriesPoint{{T: 1, V: 2}}},
	}
	m.Normalize()
	if len(m.Targets) != 1 || len(m.Series.Success24h) != 1 || m.Series.Latency24h == nil || len(m.Series.Latency24h) != 0 {
		t.Fatalf("normalized %+v", m)
	}
}

func TestOverlapSummaryDecodes(t *testing.T) {
	var o OverlapSummary
	if err := json.Unmarshal([]byte(`{"sharedShare":0.25,"likelyUpstream":{"slug":"up","shareOfOurs":0.8}}`), &o); err != nil {
		t.Fatal(err)
	}
	if o.Overlaps == nil || len(o.Overlaps) != 0 || o.ExclusiveShare.OK() || o.SharedShare != 0.25 {
		t.Fatalf("summary %+v", o)
	}
	if o.LikelyUpstream == nil || o.LikelyUpstream.Slug != "up" || o.LikelyUpstream.ShareOfOurs != 0.8 || o.LikelyUpstream.Name != nil {
		t.Fatalf("upstream %+v", o.LikelyUpstream)
	}
	if err := json.Unmarshal([]byte(`{"overlaps":[{"slug":"b","sharedIps":3}]}`), &o); err != nil {
		t.Fatal(err)
	}
	if len(o.Overlaps) != 1 || o.Overlaps[0].Slug != "b" || o.Overlaps[0].SharedIps != 3 || o.Overlaps[0].ShareOfOurs.OK() {
		t.Fatalf("overlaps %+v", o.Overlaps)
	}
	if err := json.Unmarshal([]byte(`{"overlaps":3}`), &o); err == nil {
		t.Fatal("overlaps as a number decoded")
	}
}

// Every Num field the blocks of Measured decode is NaN when its key is
// missing or null, and the value when present.
func TestMeasuredBlocksDecodeMissingAsNaN(t *testing.T) {
	var pool PoolStats
	var perf PerformanceStats
	var sp SeriesPoint
	var lp LatencyPoint
	var ip IPSample
	var inc Incident
	var geo GeoBucket
	var asn AsnBucket
	var tb TargetBenchmark
	for _, c := range []struct {
		json string
		v    any
		ok   func() bool
	}{
		{`{"countries":12}`, &pool, func() bool { return pool.Countries == 12 && !pool.UniqueIps24h.OK() }},
		{`{"latencyP50":410,"uptimePct":null}`, &perf, func() bool { return perf.LatencyP50 == 410 && !perf.UptimePct.OK() && !perf.SuccessRate.OK() }},
		{`{"t":5}`, &sp, func() bool { return sp.T == 5 && !sp.V.OK() }},
		{`{"t":5,"p90":7}`, &lp, func() bool { return lp.T == 5 && lp.P90 == 7 && !lp.P50.OK() && !lp.P99.OK() }},
		{`{"ip":"192.0.2.1","asn":64500}`, &ip, func() bool { return SV(ip.IP) == "192.0.2.1" && ip.Asn == 64500 && !ip.LatencyMs.OK() }},
		{`{"id":"i1","impact":0.3}`, &inc, func() bool { return inc.ID == "i1" && inc.Impact == 0.3 && !inc.DurationMinutes.OK() }},
		{`{"countryCode":"DE","share":0.5}`, &geo, func() bool { return geo.CountryCode == "DE" && geo.Share == 0.5 && !geo.Ips.OK() }},
		{`{"kind":"isp","ips":4}`, &asn, func() bool { return asn.Kind == "isp" && asn.Ips == 4 && !asn.Asn.OK() }},
		{`{"target":"web","requests":9}`, &tb, func() bool {
			return tb.Target == "web" && tb.Requests == 9 && !tb.SuccessRate.OK() && tb.BrowserBenchmark == nil
		}},
	} {
		if err := json.Unmarshal([]byte(c.json), c.v); err != nil {
			t.Fatalf("%T %s: %v", c.v, c.json, err)
		}
		if !c.ok() {
			t.Errorf("%T %s decoded as %+v", c.v, c.json, c.v)
		}
	}
	var pv PoolOverlap
	var lu LikelyUpstream
	for _, v := range []any{&pool, &perf, &sp, &lp, &ip, &inc, &geo, &asn, &tb, &pv, &lu} {
		if err := json.Unmarshal([]byte(`[1]`), v); err == nil {
			t.Errorf("%T: an array decoded", v)
		}
	}
}

// A browser target's figures are written inline; a network target's entry
// has none of them.
func TestTargetBenchmarkBrowserFields(t *testing.T) {
	net, err := json.Marshal(TargetBenchmark{Target: "web", SuccessRate: 0.9})
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(net, &m); err != nil {
		t.Fatal(err)
	}
	if _, ok := m["outcomes"]; ok {
		t.Errorf("network target writes outcomes: %s", net)
	}
	br, err := json.Marshal(TargetBenchmark{Target: "ai", BrowserBenchmark: &BrowserBenchmark{PageLoadP50: 1200, Outcomes: Outcomes{Ok: 1}}})
	if err != nil {
		t.Fatal(err)
	}
	m = nil
	if err := json.Unmarshal(br, &m); err != nil {
		t.Fatal(err)
	}
	if m["pageLoadP50"] != 1200.0 || m["outcomes"].(map[string]any)["ok"] != 1.0 {
		t.Errorf("browser target %s", br)
	}
	if _, ok := m["sources"]; ok {
		t.Errorf("non-group browser target writes sources: %s", br)
	}
}

func TestCoverageRowIsACompactArray(t *testing.T) {
	r := CoverageRow{CountryCode: S("DE"), Country: S("Germany"), City: S("Berlin"), Asn: 64500, Ips: 7, LatencyP50: Num(math.NaN())}
	got, err := json.Marshal(r)
	if err != nil {
		t.Fatal(err)
	}
	if want := `["DE","Germany",null,"Berlin",64500,null,null,7,null]`; string(got) != want {
		t.Fatalf("got %s, want %s", got, want)
	}
}

// MeasurementMap writes its keys in insertion order; a second Set of a key
// replaces its value in place.
func TestMeasurementMapKeepsInsertionOrder(t *testing.T) {
	var nilMap *MeasurementMap
	if nilMap.Get("a") != nil {
		t.Fatal("nil map Get")
	}
	m := NewMeasurementMap()
	if got, err := json.Marshal(m); err != nil || string(got) != "{}" {
		t.Fatalf("empty map %s %v", got, err)
	}
	b := &ProductMeasurement{Kind: "residential", Product: "b", Measured: EmptyMeasured()}
	a := &ProductMeasurement{Kind: "isp", Product: "a", Measured: EmptyMeasured()}
	m.Set("zeta", b)
	m.Set("alpha", a)
	m.Set("zeta", a)
	if len(m.Keys) != 2 || m.Keys[0] != "zeta" || m.Keys[1] != "alpha" || m.Get("zeta") != a || m.Get("missing") != nil {
		t.Fatalf("map %+v", m)
	}
	raw, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	var keys []string
	if _, err := dec.Token(); err != nil {
		t.Fatal(err)
	}
	for dec.More() {
		k, err := dec.Token()
		if err != nil {
			t.Fatal(err)
		}
		keys = append(keys, k.(string))
		var v ProductMeasurement
		if err := dec.Decode(&v); err != nil {
			t.Fatal(err)
		}
		if v.Product != "a" || v.Kind != "isp" {
			t.Errorf("%s = %+v", k, v)
		}
	}
	if len(keys) != 2 || keys[0] != "zeta" || keys[1] != "alpha" {
		t.Fatalf("keys %v", keys)
	}
	// A value that cannot be written fails the whole map.
	bad := &ProductMeasurement{Measured: EmptyMeasured()}
	bad.Pricing.Plans = []PricingPlan{{Catalog: json.RawMessage(`{`)}}
	m.Set("bad", bad)
	if _, err := json.Marshal(m); err == nil {
		t.Fatal("a broken value marshalled")
	}
}
