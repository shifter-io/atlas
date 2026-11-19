package build

import (
	"encoding/json"
	"testing"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/store"
)

var placeDE, placeEU = sp("DE"), sp("eu")

// describeCases are every event kind with the tape.ts title, detail and
// value it reads as.
var describeCases = []struct {
	name            string
	kind            string
	country, region *string
	target          *string
	data            string
	want            tapeText
}{
	{"daily started", "daily_test_started", placeDE, nil, nil, `{"countries":["DE","FR"]}`,
		tapeText{"Daily test started in DE", "2 countries", "Started"}},
	{"daily failed", "daily_test_finished", nil, placeEU, nil, `{"countries":["DE"],"requests":1234,"duration_s":30,"status":"failed"}`,
		tapeText{"Daily test failed in eu", "1 country · 1,234 requests · <1 min", "Failed"}},
	{"daily done", "daily_test_finished", nil, nil, nil, `{"requests":10,"duration_s":5400,"unique_ips":2500}`,
		tapeText{"Daily test done", "10 requests · 1h 30m", "2,500 IPs"}},
	{"hour released", "estimate_hour_released", nil, nil, nil, `{"hour":7,"day":"2026-11-02"}`,
		tapeText{"Hourly update for 07:00 UTC", "2026-11-02", "N/A"}},
	{"hour not whole", "estimate_hour_released", nil, nil, nil, `{"hour":7.5}`,
		tapeText{"Hourly update released", "", "N/A"}},
	{"pulse down", "pulse_down", placeDE, nil, nil, `{"errors":3,"requests":10,"window_minutes":15,"regions":1}`,
		tapeText{"Pulse down in DE", "3 of 10 failed · 15 min window · 1 region", "Down"}},
	{"pulse recovered", "pulse_recovered", nil, nil, nil, `{"duration_s":120}`,
		tapeText{"Pulse recovered", "down 2 min", "Recovered"}},
	{"latency rose", "latency_rise", nil, nil, nil, `{"baseline":400,"p50":1500}`,
		tapeText{"Latency rose to 1.50 s", "baseline 400 ms", "1.50 s"}},
	{"latency rose, no p50", "latency_rise", nil, nil, nil, `{}`,
		tapeText{"Latency rose", "", "N/A"}},
	{"census started", "census_started", placeDE, placeEU, sp("t1"), `{}`,
		tapeText{"Census started in DE (eu)", "t1", "Started"}},
	{"census started, nowhere", "census_started", nil, nil, nil, `{}`,
		tapeText{"Census started", "", "Started"}},
	{"census stalled", "census_finished", placeDE, nil, nil, `{"requests":2000,"duration_s":30,"stop_reason":"stalled"}`,
		tapeText{"Census stopped in DE: network down", "2,000 requests · <1 min", "Down"}},
	{"census done", "census_finished", nil, nil, nil, `{"unique_ips":12}`,
		tapeText{"Census done", "", "12 IPs"}},
	{"census done, no IPs", "census_finished", nil, nil, nil, `{}`,
		tapeText{"Census done", "", "N/A"}},
	{"census rerun", "census_retry", nil, nil, nil, `{"reason":"no_success"}`,
		tapeText{"Census rerun", "after no success", "Rerun"}},
	{"census rerun, no reason", "census_retry", nil, nil, nil, `{}`,
		tapeText{"Census rerun", "", "Rerun"}},
	{"census skipped", "census_skipped", nil, nil, nil, `{"reason":"busy"}`,
		tapeText{"Census skipped", "busy", "Skipped"}},
	{"pulse", "pulse_finished", placeDE, nil, nil, `{"requests":50,"p50":250.4,"challenges":1,"blocks":2,"success_rate":0.5}`,
		tapeText{"Pulse in DE", "50 requests · p50 250 ms · 1 challenge · 2 blocks", "50%"}},
	{"pulse, value only", "pulse_finished", nil, nil, nil, `{"challenges":0,"blocks":0,"value":0.75}`,
		tapeText{"Pulse", "", "75%"}},
	{"pulse, nothing", "pulse_finished", nil, nil, nil, `{"challenges":3}`,
		tapeText{"Pulse", "3 challenges", "N/A"}},
	{"success drop", "success_drop", placeDE, nil, nil, `{"success_rate":0.5,"baseline":0.75}`,
		tapeText{"Success dropped to 50% in DE", "baseline 75%", "50%"}},
	{"success drop, no rate", "success_drop", nil, nil, nil, `{}`,
		tapeText{"Success dropped", "", "N/A"}},
	{"latency spike", "latency_spike", nil, nil, nil, `{"value":800,"baseline":300}`,
		tapeText{"Latency spiked to 800 ms", "baseline 300 ms", "800 ms"}},
	{"latency spike, p50", "latency_spike", nil, placeEU, nil, `{"p50":90,"value":800}`,
		tapeText{"Latency spiked to 90 ms in eu", "", "90 ms"}},
	{"latency spike, nothing", "latency_spike", nil, nil, nil, `{}`,
		tapeText{"Latency spiked", "", "N/A"}},
	{"block spike", "block_spike", nil, nil, nil, `{"blocks":40,"baseline":0.25}`,
		tapeText{"Blocks spiked", "baseline 25%", "40 blocks"}},
	{"block spike, no blocks", "block_spike", nil, nil, nil, `{}`,
		tapeText{"Blocks spiked", "", "N/A"}},
	{"down in census", "down", placeDE, nil, nil, `{"source":"census"}`,
		tapeText{"Down in DE (during census)", "", "Down"}},
	{"down", "down", nil, nil, nil, `{"source":"pulse"}`,
		tapeText{"Down", "", "Down"}},
	{"recovered somewhere", "recovered", nil, nil, nil, `{"region":"apac"}`,
		tapeText{"Recovered in apac", "", "Recovered"}},
	{"recovered", "recovered", nil, nil, nil, `{}`,
		tapeText{"Recovered", "", "Recovered"}},
	{"new hosting ASN", "new_hosting_asn", nil, nil, nil, `{"name":"Example Hosting"}`,
		tapeText{"DC ASN seen", "Example Hosting", "New"}},
	{"IP overlap", "ip_overlap", nil, nil, nil, `{"value":0.25}`,
		tapeText{"IP overlap seen", "", "25%"}},
	{"IP overlap, no value", "ip_overlap", nil, nil, nil, `{}`,
		tapeText{"IP overlap seen", "", "N/A"}},
	{"node offline", "node_offline", nil, nil, nil, `{"region":"apac"}`,
		tapeText{"Node offline in apac", "", "Offline"}},
	{"no node", "no_node", sp(""), sp(""), nil, `{}`,
		tapeText{"No node available", "", "No node"}},
	{"unknown kind", "some_new_kind", nil, nil, nil, `{}`,
		tapeText{"some new kind", "", "N/A"}},
}

// Every event kind reads as the tape.ts title, detail and value, with the
// place from the country, else the region, else data.region.
func TestDescribeEveryEventKind(t *testing.T) {
	for _, c := range describeCases {
		t.Run(c.name, func(t *testing.T) {
			var data map[string]any
			if err := json.Unmarshal([]byte(c.data), &data); err != nil {
				t.Fatal(err)
			}
			e := &store.EventJSON{Kind: c.kind, Country: c.country, Region: c.region, Target: c.target}
			if got := describe(e, data); got != c.want {
				t.Errorf("describe = %+v, want %+v", got, c.want)
			}
		})
	}
}

// TapeFromEvents names each row's network (the last product row's name, else
// the provider id), its slug, tab, proxy type and age.
func TestTapeFromEventsNamesTheNetworkKindAndAge(t *testing.T) {
	products := []TapeProduct{{"p1", "Old", "census"}, {"p1", "Alpha", "isp"}, {"p2", "Beta", "datacenter"}}
	ev := func(id int64, at, kind, category, product, provider, data string) store.EventJSON {
		return store.EventJSON{ID: id, At: at, Kind: kind, Category: category, Product: product, Provider: provider,
			Country: sp("DE"), Signal: 2, Data: json.RawMessage(data)}
	}
	events := []store.EventJSON{
		ev(5, "2026-11-03T04:00:00.000000+00:00", "down", "incident", "p2/mobile", "p2", `{"source":"census"}`),
		ev(4, "2026-11-03T04:05:00.000000+00:00", "census_skipped", "weird", "p1/", "p1", `{"reason":"busy"}`),
		ev(3, "not a time", "census_retry", "census", "", "", `{"reason":"no_success"}`),
		ev(2, "2026-11-03T03:00:00.000000+00:00", "census_skipped", "audit", "p3", "p3", `[1,2]`),
		ev(1, "2026-11-03T03:00:00.000000+00:00", "pulse_finished", "pulse", "p2", "p2", `{}`),
	}
	got := TapeFromEvents(events, products, "2026-11-03T04:01:00.000Z", map[string]string{"p2": "beta-net"})
	want := []struct {
		id, kind, slug, name, product, title, detail string
		ago                                          Num
	}{
		{"event-5", "incident", "beta-net", "Beta", "mobile", "Down in DE (during census)", "Beta", 60},
		{"event-4", "incident", "p1", "Alpha", "residential", "Census skipped in DE", "Alpha · busy", 0},
		{"event-3", "census", "", "", "residential", "Census rerun in DE", "after no success", 0},
		{"event-2", "audit", "p3", "p3", "residential", "Census skipped in DE", "p3", 3660},
		{"event-1", "pulse", "beta-net", "Beta", "datacenter", "Pulse in DE", "Beta", 3660},
	}
	if len(got) != len(want) {
		t.Fatalf("%d rows, want %d", len(got), len(want))
	}
	for i, w := range want {
		g := got[i]
		if g.ID != w.id || g.Kind != w.kind || g.ProviderSlug != w.slug || g.ProviderName != w.name || g.Product != w.product ||
			g.Title != w.title || g.Detail != w.detail || g.AgoSeconds != w.ago {
			t.Errorf("row %d = %+v, want %+v", i, g, w)
		}
		if g.Signal != 2 || g.Source != model.SourceMeasured || model.SV(g.At) != events[i].At || g.NA == nil {
			t.Errorf("row %d signal %v, source %q, at %v, na %v", i, g.Signal, g.Source, g.At, g.NA)
		}
	}
}
