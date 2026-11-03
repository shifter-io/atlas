// Package testscale builds a published snapshot as large as a real daily
// build, for tests that bound response sizes.
//
// The QA-1 build (18 networks, 40 requested countries each) published one
// product group per network and requested country, each with three windows
// whose capped lists hold their top rows (country up to 1000 rows, the other
// breakdowns 25, pool geo and ASNs 50). Inputs repeats the every-request
// fixture's product into that many groups with lists of those lengths and
// the long tails a capped build keeps in its typed rows. Like the QA-1
// database it also holds what is not in the snapshot: framework providers
// (21, three of them with a product but no published group, so nearly every
// field is N/A), their products, the framework targets and the test events
// (556 hour releases and three pulse events). Its site.* catalog tables were
// empty, so Inputs has no catalog or demo rows either.
package testscale

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
)

// Shape is how large a synthetic build is.
type Shape struct {
	Networks  int // framework providers
	Countries int // requested countries per network: one product group each
	Exits     int // exit countries in each window's country breakdown
	Tail      int // full rows of each capped region, city and ASN list
	// Unmeasured framework providers: a product, no published group.
	Unmeasured int
	Events     int // test events
}

// QA1 is the QA-1 build: 18 networks x 40 requested countries, about 240
// exit countries per window.
var QA1 = Shape{Networks: 18, Countries: 40, Exits: 240, Tail: 200, Unmeasured: 3, Events: 559}

// capped is how many rows the framework's [payload] settings keep.
var capped = map[string]int{"region": 25, "city": 25, "asn": 25, "country_asn": 25, "city_asn": 25}

// Inputs is a store.Inputs holding only a published snapshot of the shape.
func Inputs(shape Shape) (*store.Inputs, error) {
	_, file, _, _ := runtime.Caller(0)
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(file), "..", "..", "..", "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil {
		return nil, err
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil, err
	}
	template := doc["products"].([]any)[0].(map[string]any)
	products := []any{}
	places := map[store.PlaceWindow][]store.PlaceRow{}
	for n := range shape.Networks {
		for c := range shape.Countries {
			id := n*1000 + c + 1
			cohort := fmt.Sprintf("daily-%03d", c)
			p, err := clone(template)
			if err != nil {
				return nil, err
			}
			p["id"], p["product_id"], p["cohort"] = fmt.Sprintf("%d:%s", id, cohort), id, cohort
			p["provider"], p["name"] = fmt.Sprintf("net%02d", n), fmt.Sprintf("Net %02d residential", n)
			p["criteria"] = map[string]any{"kind": "residential", "country": countryCode(c)}
			// A daily test's group: no census or pulse (uptime, outages), no
			// price evidence (QA-1 had none) and no load benchmark.
			for _, k := range []string{"census", "outages", "pricing"} {
				delete(p, k)
			}
			for label, w := range p["windows"].(map[string]any) {
				hours := map[string]int64{"24h": 24, "7d": 168, "30d": 720}[label]
				for _, k := range []string{"uptime", "benchmark", "census_overlap", "evidence", "evidence_sources", "ip_samples"} {
					delete(w.(map[string]any), k)
				}
				fill(w.(map[string]any), shape, n, c)
				places[store.PlaceWindow{Product: int64(id), Cohort: cohort, Hours: hours}] = tails(shape, n)
			}
			products = append(products, p)
		}
	}
	doc["products"] = products
	payload, err := json.Marshal(doc)
	if err != nil {
		return nil, err
	}
	in := &store.Inputs{Snapshot: &store.SnapshotRow{BuildID: 1, ContentHash: "scale", Payload: payload, Places: places},
		Events: events(shape), EventsComplete: true, NotableComplete: true}
	for n := range shape.Networks + shape.Unmeasured {
		id := fmt.Sprintf("net%02d", n)
		in.FrameworkProviders = append(in.FrameworkProviders, id)
		in.FrameworkProducts = append(in.FrameworkProducts, store.FrameworkProduct{ID: int32(n + 1), Provider: id, Name: "residential"})
	}
	for i, t := range doc["targets"].([]any) {
		label := t.(map[string]any)["label"].(string)
		in.FrameworkTargets = append(in.FrameworkTargets, store.FrameworkTarget{ID: t.(map[string]any)["id"].(string), Label: &label, SortOrder: int32(i)})
	}
	return in, nil
}

// events is the shape's test events, newest first: hour releases per network
// and requested country, then a pulse outage, its recovery and a latency rise.
func events(shape Shape) []store.Event {
	at := time.Date(2026, 11, 3, 4, 0, 0, 0, time.UTC)
	out := []store.Event{}
	for i := range shape.Events {
		provider := fmt.Sprintf("net%02d", i%max(shape.Networks, 1))
		product, country := "residential", countryCode(i%max(shape.Countries, 1))
		kind, severity := "estimate_hour_released", "info"
		data := fmt.Sprintf(`{"day":"2026-11-02","hour":%d,"formula_version":1,"unique_ips":%d,"requests":%d}`, i%24, 500+i, 3000+i)
		switch i {
		case 0, 1, 2:
			kind, severity = [...]string{"pulse_down", "pulse_recovered", "latency_rise"}[i], "warning"
			data = `{"source":"pulse","p50_ms":900,"baseline_ms":400}`
		}
		out = append(out, store.Event{ID: int64(shape.Events - i), At: at.Add(-time.Duration(i) * time.Minute), Kind: kind,
			Provider: &provider, Product: &product, Country: &country, Signal: 1, Severity: severity, Data: json.RawMessage(data)})
	}
	return out
}

func clone(v map[string]any) (map[string]any, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	return out, json.Unmarshal(b, &out)
}

func countryCode(i int) string { return string(rune('A'+i/26%26)) + string(rune('A'+i%26)) }

func row(i, ips int, label map[string]any) map[string]any {
	r := map[string]any{"ips": ips, "observations": ips * 2, "completed": ips * 3, "successes": ips * 2,
		"success_rate": 2.0 / 3, "challenges": 0, "blocks": 0, "bytes_up": ips * 900, "bytes_down": ips * 40000,
		"p50": 400 + i, "p90": 900 + i, "p99": 2000 + i}
	for k, v := range label {
		r[k] = v
	}
	return r
}

func place(i int) map[string]any {
	cc := countryCode(i % 250)
	return map[string]any{"country_code": cc, "country": "Country " + cc, "region_code": fmt.Sprintf("R%d", i%40),
		"region": fmt.Sprintf("Region %d", i%40), "city": fmt.Sprintf("City %d", i)}
}

func network(i int) map[string]any {
	return map[string]any{"asn": 64512 + i, "name": fmt.Sprintf("Example Networks %d", i), "usage_type": "isp"}
}

// fill gives a window the capped lists of the shape.
func fill(w map[string]any, shape Shape, n, c int) {
	lists := map[string][]any{}
	entries := map[string]any{}
	for name, keep := range map[string]int{"country": shape.Exits, "region": shape.Tail, "city": shape.Tail, "asn": shape.Tail,
		"country_asn": shape.Tail, "city_asn": shape.Tail} {
		rows, n := []any{}, keep
		if limit, ok := capped[name]; ok && limit < n {
			n = limit
		}
		for i := range n {
			label := place(i + c)
			switch name {
			case "country":
				label = map[string]any{"country_code": countryCode((i + c) % 250), "country": "Country"}
			case "region":
				delete(label, "city")
			case "asn":
				label = network(i + c)
			case "country_asn":
				label = map[string]any{"country_code": label["country_code"], "country": label["country"]}
				for k, v := range network(i) {
					label[k] = v
				}
			case "city_asn":
				for k, v := range network(i) {
					label[k] = v
				}
			}
			rows = append(rows, row(i, 1000-i, label))
		}
		lists[name], entries[name] = rows, keep
	}
	w["breakdowns"], w["breakdown_entries"] = lists, entries
	pool := w["pool"].(map[string]any)
	geo, countries, asns := []any{}, []any{}, []any{}
	for i := range 50 {
		geo = append(geo, map[string]any{"country_code": countryCode(i), "country": "Country", "region": "Region", "region_code": "R", "city": fmt.Sprintf("City %d", i), "ips": 50 - i, "observations": 50})
		asns = append(asns, map[string]any{"asn": 64512 + i, "name": "Example", "usage_type": "isp", "ips": 50 - i, "observations": 50})
	}
	for i := range shape.Exits {
		countries = append(countries, map[string]any{"country_code": countryCode(i), "country": "Country", "ips": shape.Exits - i, "observations": 9})
	}
	pool["geo"], pool["countries_breakdown"], pool["asns_breakdown"] = geo, countries, asns
	pool["list_entries"] = map[string]any{"geo": shape.Tail, "countries_breakdown": shape.Exits, "asns_breakdown": shape.Tail}
}

// tails is the typed region, city and ASN rows of one window.
func tails(shape Shape, n int) []store.PlaceRow {
	out := []store.PlaceRow{}
	for i := range shape.Tail {
		p := place(i + n)
		cc, country, rc, region, city := p["country_code"].(string), p["country"].(string), p["region_code"].(string), p["region"].(string), p["city"].(string)
		nw := network(i + n)
		name := nw["name"].(string)
		ips, completed, successes := float64(1000-i), float64(3*(1000-i)), float64(2*(1000-i))
		out = append(out,
			store.PlaceRow{Dimension: "region", CountryCode: &cc, Country: &country, RegionCode: &rc, Region: &region, Asn: nan(), Ips: ips, Completed: completed, Successes: successes, P50: float64(400 + i)},
			store.PlaceRow{Dimension: "city", CountryCode: &cc, Country: &country, RegionCode: &rc, Region: &region, City: &city, Asn: nan(), Ips: ips, Completed: completed, Successes: successes, P50: float64(400 + i)},
			store.PlaceRow{Dimension: "asn", Asn: float64(64512 + i + n), Name: &name, Ips: ips, Completed: completed, Successes: successes, P50: float64(400 + i)})
	}
	return out
}

func nan() float64 { var z float64; return z / z }
