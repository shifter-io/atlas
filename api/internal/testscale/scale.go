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
// empty, so Inputs has no catalog or demo rows either. QA1 also carries the
// nine browser pulse targets in every window, so size bounds hold with them.
//
// Current and Parts (or, for a large shape, CurrentGroups) give a payload as
// the framework stores it since calculated v8: in parts, the daily groups'
// 24-hour series sparse.
package testscale

import (
	"bytes"
	"encoding/json"
	"fmt"
	"maps"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
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
	// NoFleetAll leaves out the every-group fleet entries (cohort "*"), as
	// in snapshots built before the framework published them.
	NoFleetAll bool
	// Browser adds the nine browser pulse targets to every window (AddBrowser).
	Browser bool
	// SuccessScores adds the success scores to every window (AddSuccessScores).
	SuccessScores bool
	// Gaps leaves each network's own series points unmeasured (addGaps), so
	// its curve has its own labels, as in production.
	Gaps bool
	// CityAsn gives each network's headline group (its first requested
	// country) its lists as store.SnapshotRow.Full puts them back from the
	// typed rows: a whole city x ASN list of this many rows and a whole pool
	// ASN list (Tail rows) in every window. Zero leaves them capped.
	CityAsn int
}

// FleetUniqueIps is the distinct IPs of every every-group fleet entry, and
// FleetBucketIps the distinct IPs of its first series bucket (the next bucket
// has one more, and so on).
const (
	FleetUniqueIps = 4321
	FleetBucketIps = 100
)

// QA1 is the QA-1 build: 18 networks x 40 requested countries, about 240
// exit countries per window, and each network's headline group with a whole
// city x ASN list of 1,000 rows per window (real builds have tens of
// thousands; the dataset file carries them whole).
var QA1 = Shape{Networks: 18, Countries: 40, Exits: 240, Tail: 200, Unmeasured: 3, Events: 559, Browser: true, CityAsn: 1000}

// capped is how many rows the framework's [payload] settings keep.
var capped = map[string]int{"region": 25, "city": 25, "asn": 25, "country_asn": 25, "city_asn": 25}

// Inputs is a store.Inputs holding only a published snapshot of the shape.
func Inputs(shape Shape) (*store.Inputs, error) {
	doc, places, err := shapeDoc(shape, func(map[string]any) {})
	if err != nil {
		return nil, err
	}
	return docInputs(shape, doc, places)
}

// docInputs is Inputs of a shape's snapshot document.
func docInputs(shape Shape, doc map[string]any, places map[store.PlaceWindow]*store.List) (*store.Inputs, error) {
	payload, err := json.Marshal(doc)
	if err != nil {
		return nil, err
	}
	in := &store.Inputs{Snapshot: &store.SnapshotRow{BuildID: 1, ContentHash: "scale", Payload: payload, Places: places},
		Events: events(shape), EventsComplete: true, NotableComplete: true}
	frameworkRows(in, doc, shape)
	return in, nil
}

// shapeDoc is the shape's snapshot payload, decoded but for its product
// groups, which are each encoded alone after edit (marshalGroups), and the
// typed tails of all their windows.
func shapeDoc(shape Shape, edit func(map[string]any)) (map[string]any, map[store.PlaceWindow]*store.List, error) {
	raw, err := fixture()
	if err != nil {
		return nil, nil, err
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil, nil, err
	}
	template := doc["products"].([]any)[0].(map[string]any)
	products, places, err := productGroups(template, shape)
	if err != nil {
		return nil, nil, err
	}
	doc["products"] = products
	if err := snapshotDoc(doc, template, shape); err != nil {
		return nil, nil, err
	}
	if shape.Gaps { // after edit, which may fill every point
		inner := edit
		edit = func(g map[string]any) {
			if inner != nil {
				inner(g)
			}
			addGaps(g)
		}
	}
	return doc, places, marshalGroups(doc, edit)
}

// productGroups is every product group of the shape, built in parallel, and
// the typed tails of all their windows.
func productGroups(template map[string]any, shape Shape) ([]any, map[store.PlaceWindow]*store.List, error) {
	// Network n's group for requested country c is products[n*Countries+c].
	products := make([]any, shape.Networks*shape.Countries)
	windows := make([]map[store.PlaceWindow]*store.List, len(products))
	err := each(len(products), func(i int) error {
		var err error
		products[i], windows[i], err = group(template, shape, i/shape.Countries, i%shape.Countries)
		return err
	})
	if err != nil {
		return nil, nil, err
	}
	places := map[store.PlaceWindow]*store.List{}
	for _, w := range windows {
		maps.Copy(places, w)
	}
	return products, places, nil
}

// snapshotDoc adds the shape's browser targets and every-group fleet
// entries to doc, as the framework publishes them.
func snapshotDoc(doc, template map[string]any, shape Shape) error {
	if shape.Browser {
		if err := AddBrowser(doc); err != nil {
			return err
		}
	}
	if shape.SuccessScores {
		if err := AddSuccessScores(doc); err != nil {
			return err
		}
	}
	if !shape.NoFleetAll {
		fleet, err := fleetAll(doc["fleet"], template, shape)
		if err != nil {
			return err
		}
		doc["fleet"] = fleet
	}
	return nil
}

// frameworkRows gives in the framework providers, products and targets.
func frameworkRows(in *store.Inputs, doc map[string]any, shape Shape) {
	for n := range shape.Networks + shape.Unmeasured {
		id := fmt.Sprintf("net%02d", n)
		in.FrameworkProviders = append(in.FrameworkProviders, id)
		in.FrameworkProducts = append(in.FrameworkProducts, store.FrameworkProduct{ID: int32(n + 1), Provider: id, Name: "residential"})
	}
	for i, t := range doc["targets"].([]any) {
		label := t.(map[string]any)["label"].(string)
		engine, _ := t.(map[string]any)["engine"].(string)
		in.FrameworkTargets = append(in.FrameworkTargets, store.FrameworkTarget{ID: t.(map[string]any)["id"].(string), Label: &label, SortOrder: int32(i), Engine: engine})
	}
}

// group is network n's product group for requested country c, a copy of
// template with the shape's lists, and the typed tails of its windows.
func group(template map[string]any, shape Shape, n, c int) (map[string]any, map[store.PlaceWindow]*store.List, error) {
	id := n*1000 + c + 1
	cohort := fmt.Sprintf("daily-%03d", c)
	p, err := clone(template)
	if err != nil {
		return nil, nil, err
	}
	p["id"], p["product_id"], p["cohort"] = fmt.Sprintf("%d:%s", id, cohort), id, cohort
	p["provider"], p["name"] = fmt.Sprintf("net%02d", n), fmt.Sprintf("Net %02d residential", n)
	p["criteria"] = map[string]any{"kind": "residential", "country": countryCode(c)}
	// A daily test's group: no census or pulse (uptime, outages), no
	// price evidence (QA-1 had none) and no load benchmark.
	for _, k := range []string{"census", "outages", "pricing"} {
		delete(p, k)
	}
	places := map[store.PlaceWindow]*store.List{}
	for label, w := range p["windows"].(map[string]any) {
		hours := map[string]int64{"24h": 24, "7d": 168, "30d": 720}[label]
		for _, k := range []string{"uptime", "benchmark", "census_overlap", "evidence", "evidence_sources", "ip_samples"} {
			delete(w.(map[string]any), k)
		}
		fill(w.(map[string]any), shape, c)
		places[store.PlaceWindow{Product: int64(id), Cohort: cohort, Hours: hours}] = tails(shape, n)
	}
	return p, places, nil
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

// fleetAll appends the every-group fleet entries (proxy-index fleet_totals):
// per window the template's metrics and series, with the distinct IP counts
// above in place of the template's.
func fleetAll(fleet any, template map[string]any, shape Shape) ([]any, error) {
	out, _ := fleet.([]any)
	for _, label := range []string{"24h", "7d", "30d"} {
		w := template["windows"].(map[string]any)[label].(map[string]any)
		series := []any{}
		for i, r := range w["series"].([]any) {
			row, err := clone(r.(map[string]any))
			if err != nil {
				return nil, err
			}
			row["unique_ips"] = FleetBucketIps + i
			series = append(series, row)
		}
		out = append(out, map[string]any{"cohort": "*", "window": label, "products": shape.Networks * shape.Countries,
			"providers": shape.Networks, "metrics": w["metrics"], "unique_ips": FleetUniqueIps, "series": series})
	}
	return out, nil
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
func fill(w map[string]any, shape Shape, c int) {
	whole := shape.CityAsn > 0 && c == 0
	w["breakdowns"], w["breakdown_entries"] = breakdowns(shape, c, whole)
	fillPool(w["pool"].(map[string]any), shape, whole)
}

// breakdowns is a window's breakdown lists, capped like the framework's
// unless whole, and each list's full row count.
func breakdowns(shape Shape, c int, whole bool) (map[string][]any, map[string]any) {
	lists := map[string][]any{}
	entries := map[string]any{}
	cityAsn := shape.Tail
	if whole {
		cityAsn = shape.CityAsn
	}
	for name, keep := range map[string]int{"country": shape.Exits, "region": shape.Tail, "city": shape.Tail, "asn": shape.Tail,
		"country_asn": shape.Tail, "city_asn": cityAsn} {
		rows, n := []any{}, keep
		if limit, ok := capped[name]; ok && limit < n && !(whole && name == "city_asn") {
			n = limit
		}
		for i := range n {
			rows = append(rows, row(i, max(1000-i, 1), breakdownLabel(name, i, c)))
		}
		lists[name], entries[name] = rows, keep
	}
	return lists, entries
}

// breakdownLabel is the label columns of row i of breakdown name in the
// group for requested country c.
func breakdownLabel(name string, i, c int) map[string]any {
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
		maps.Copy(label, network(i))
	case "city_asn":
		maps.Copy(label, network(i))
	}
	return label
}

// fillPool gives a window's pool its geo, country and ASN lists: the ASN
// list whole (Tail rows) when whole, else 50 rows.
func fillPool(pool map[string]any, shape Shape, whole bool) {
	geo, countries, asns := []any{}, []any{}, []any{}
	for i := range 50 {
		geo = append(geo, map[string]any{"country_code": countryCode(i), "country": "Country", "region": "Region", "region_code": "R", "city": fmt.Sprintf("City %d", i), "ips": 50 - i, "observations": 50})
	}
	asnRows := 50
	if whole {
		asnRows = shape.Tail
	}
	for i := range asnRows {
		asns = append(asns, map[string]any{"asn": 64512 + i, "name": "Example", "usage_type": "isp", "ips": asnRows - i, "observations": 50})
	}
	for i := range shape.Exits {
		countries = append(countries, map[string]any{"country_code": countryCode(i), "country": "Country", "ips": shape.Exits - i, "observations": 9})
	}
	pool["geo"], pool["countries_breakdown"], pool["asns_breakdown"] = geo, countries, asns
	pool["list_entries"] = map[string]any{"geo": shape.Tail, "countries_breakdown": shape.Exits, "asns_breakdown": shape.Tail}
}

// tails is the typed region, city and ASN rows of a network's windows, one
// list each network shares (built once).
func tails(shape Shape, n int) *store.List {
	key := [2]int{shape.Tail, n}
	if l, ok := tailLists.Load(key); ok {
		return l.(*store.List)
	}
	rows := map[string][]store.ListRow{}
	for i := range shape.Tail {
		p := place(i + n)
		cc, country, rc, region, city := p["country_code"].(string), p["country"].(string), p["region_code"].(string), p["region"].(string), p["city"].(string)
		nw := network(i + n)
		name, asn := nw["name"].(string), int64(64512+i+n)
		ips := int64(1000 - i)
		counters := store.ListRow{Ips: ips, Observations: 2 * ips, Completed: 3 * ips, Successes: 2 * ips, P50: int32(400 + i), P90: -1, P99: -1}
		r, g, a := counters, counters, counters
		r.Place = &store.Place{CountryCode: &cc, Country: &country, RegionCode: &rc, Region: &region}
		g.Place = &store.Place{CountryCode: &cc, Country: &country, RegionCode: &rc, Region: &region, City: &city}
		a.Network = &store.Network{Asn: &asn, Name: &name}
		rows["region"], rows["city"], rows["asn"] = append(rows["region"], r), append(rows["city"], g), append(rows["asn"], a)
	}
	l, _ := tailLists.LoadOrStore(key, store.NewList(rows))
	return l.(*store.List)
}

var tailLists sync.Map

// fixture is the every-request fixture's payload.
func fixture() ([]byte, error) {
	_, file, _, _ := runtime.Caller(0)
	return os.ReadFile(filepath.Join(filepath.Dir(file), "..", "..", "fixtures", "measured", "snapshot-every-request.json"))
}

// EveryRequest is the every-request fixture's payload as Current writes it,
// with a second group: a daily copy of its census/pulse group (cohort
// "daily-us", no schedule), as a build has both.
func EveryRequest() ([]byte, error) {
	raw, err := fixture()
	if err != nil {
		return nil, err
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil, err
	}
	products := doc["products"].([]any)
	daily, err := clone(products[0].(map[string]any))
	if err != nil {
		return nil, err
	}
	criteria := daily["criteria"].(map[string]any)
	delete(criteria, "schedule")
	daily["cohort"], daily["id"] = "daily-us", fmt.Sprintf("%v:daily-us", daily["product_id"])
	doc["products"] = append(products, daily)
	payload, err := json.Marshal(doc)
	if err != nil {
		return nil, err
	}
	return Current(payload)
}

// Current gives every empty point of the payload's 24-hour series
// (completed 0, unique_ips null) the keys proxy-index aggregate.series()
// writes now and the every-request fixture predates: uptime, and
// latency_under_load_ms when the window has a latency target.
func Current(payload []byte) ([]byte, error) {
	doc, groups, err := eachGroup(payload, current)
	if err != nil {
		return nil, err
	}
	doc["products"] = append(append([]byte("["), bytes.Join(groups, []byte(","))...), ']')
	return json.Marshal(doc)
}

// current is Current for one group.
func current(group map[string]any) {
	for _, w := range dayWindows(group) {
		latency, _ := w["latency"].(map[string]any)
		timed, _ := latency["target"].(string)
		series, _ := w["series"].([]any)
		for _, point := range series {
			pt := point.(map[string]any)
			if pt["completed"] != 0.0 || pt["unique_ips"] != nil {
				continue
			}
			pt["uptime"] = nil
			if timed != "" {
				pt["latency_under_load_ms"] = map[string]any{"p50": nil, "p90": nil, "p95": nil, "p99": nil}
			}
		}
	}
}

// RunsPerPart is the most runs one 'runs' part holds (proxy-index
// calculated/parts.py RUNS_PER_PART).
const RunsPerPart = 2000

// Parts is the payload as calculated v8 stores it (contract K1): its head
// (every top-level key except products and runs, plus "parts") and its
// product and runs parts. With sparse, each daily group's 24-hour series is
// as proxy-index calculated/parts.py sparse() leaves it (a point with
// neither unique_ips nor uptime is left out; census/pulse groups, whose
// criteria name a schedule, keep every point).
func Parts(payload []byte, sparse bool) (head []byte, products, runs [][]byte, err error) {
	edit := func(map[string]any) {}
	if sparse {
		edit = sparseSeries
	}
	doc, products, err := eachGroup(payload, edit)
	if err != nil {
		return nil, nil, nil, err
	}
	head, runs, err = headAndRuns(doc, len(products))
	return head, products, runs, err
}

// dayWindows is the group's 24-hour windows.
func dayWindows(group map[string]any) []map[string]any {
	out := []map[string]any{}
	windows, _ := group["windows"].(map[string]any)
	for _, entry := range windows {
		w, _ := entry.(map[string]any)
		if period, _ := w["period"].(map[string]any); period["hours"] == 24.0 {
			out = append(out, w)
		}
	}
	return out
}

// sparseSeries leaves a daily group's empty 24-hour points out.
func sparseSeries(group map[string]any) {
	if criteria, _ := group["criteria"].(map[string]any); criteria["schedule"] != nil {
		return
	}
	for _, w := range dayWindows(group) {
		series, ok := w["series"].([]any)
		if !ok {
			continue
		}
		kept := []any{}
		for _, point := range series {
			if pt := point.(map[string]any); pt["unique_ips"] != nil || pt["uptime"] != nil {
				kept = append(kept, pt)
			}
		}
		w["series"] = kept
	}
}
