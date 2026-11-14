package testscale

import (
	"encoding/json"
	"fmt"

	"github.com/Zeus-Development/atlas/api/internal/store"
)

// WeekShape is a build as seven days of production data shape it
// (PAGE-LATENCY-1): a payload v4 build whose every network window has a list
// of the sizes below. A list's rows are the same in every network window, so
// a database writes them once and copies them (testdb.PublishV4With): what a
// page's time depends on is how many rows a list has, not their names.
type WeekShape struct {
	NetworkShape
	Regions, Cities, Asns, CityAsns int // rows per network window
}

// Week is the shape of the page latency test: 18 networks with 249 requested
// and exit countries each, every network with 9 browser targets (the AI
// group target among them) and the AI group's 3 sources, on two engines with
// their scores; 24-hour series at 5-minute points; per network and window
// 30,000 City x ASN rows, 30,000 cities, 9,000 ASNs and 1,000 regions; and
// 2,500 test events.
var Week = WeekShape{NetworkShape: NetworkShape{Shape: Shape{Networks: 18, Countries: 1, Exits: 249, Tail: 25,
	Events: 2500, Browser: true}, Requested: 249}, Regions: 1000, Cities: 30000, Asns: 9000, CityAsns: 30000}

// weekTargets are the browser targets the week adds to BrowserTargets: with
// them a network has 9 browser targets besides the group's sources.
var weekTargets = []BrowserTarget{
	{"booking_search", "Travel search results", "https://www.booking.com/searchresults.html", 90, ""},
	{"zillow_listing", "Real estate listing", "https://www.zillow.com/homedetails/", 91, ""},
}

// Engines are the browser engines of the week's targets: every browser
// target has the figures and scores of each.
var Engines = []string{"camoufox", "chrome"}

// Payload is the week as a snapshot payload before v4 (as Current writes
// it), one product group per network; testdb.PublishV4With publishes it
// with Requested countries and the Lists rows.
func (w WeekShape) Payload() ([]byte, error) {
	doc, _, err := weekDoc(w.Shape)
	if err != nil {
		return nil, err
	}
	return json.Marshal(doc)
}

// WeekInputs is Inputs with the week's browser targets and engines: shape's
// windows hold the week's browser figures, without its database lists.
func WeekInputs(shape Shape) (*store.Inputs, error) {
	doc, places, err := weekDoc(shape)
	if err != nil {
		return nil, err
	}
	return docInputs(shape, doc, places)
}

// weekDoc is shape's snapshot document with the week's browser targets.
func weekDoc(shape Shape) (map[string]any, map[store.PlaceWindow]*store.List, error) {
	doc, places, err := shapeDoc(shape, weekGroup)
	if err != nil {
		return nil, nil, err
	}
	for _, t := range weekTargets {
		doc["targets"] = append(doc["targets"].([]any), map[string]any{"id": t.ID, "url": t.URL, "label": t.Label, "version": "1", "engine": "browser"})
	}
	return doc, places, nil
}

// weekGroup gives a product group the week's extra browser targets, every
// browser target its second engine, and its 24-hour series a measured value
// at every 5-minute point.
func weekGroup(g map[string]any) {
	for _, w := range g["windows"].(map[string]any) {
		targets := w.(map[string]any)["targets"].(map[string]any)
		for _, t := range weekTargets {
			entry, _ := clone(targets["generic_https"].(map[string]any))
			browserEntry(entry, t)
			targets[t.ID] = entry
		}
		for _, entry := range targets {
			if b, ok := entry.(map[string]any)["browser"].(map[string]any); ok {
				engines := b["engines"].(map[string]any)
				for _, e := range Engines {
					engines[e] = engines["camoufox"]
				}
			}
		}
		overall := map[string]any{}
		for _, e := range Engines {
			overall[e] = BrowserOverallScore()
		}
		w.(map[string]any)["browser_overall"] = overall
	}
	for _, w := range dayWindows(g) {
		for i, point := range w["series"].([]any) {
			pt := point.(map[string]any)
			pt["completed"], pt["successes"], pt["success_rate"], pt["unique_ips"] = 120, 110, 110.0/120, 50+i
			pt["latency_ms"] = map[string]any{"p50": 400 + i, "p90": 900 + i, "p95": 1200 + i, "p99": 2000 + i}
		}
	}
}

// Lists is the rows of every network window's list, by payload breakdown
// name: the shape's exit countries, regions, cities, ASNs and City x ASN
// rows, most IPs first, labelled as the other shapes' rows are.
func (w WeekShape) Lists() map[string][]store.NetRow {
	rows := map[string][]store.NetRow{}
	counts := map[string]int{"country": w.Exits, "region": w.Regions, "city": w.Cities, "asn": w.Asns, "city_asn": w.CityAsns}
	for name, n := range counts {
		rows[name] = make([]store.NetRow, n)
		for i := range n {
			ips := int64(n - i)
			r := store.NetRow{Ips: ips, Completed: 3 * ips, Successes: 2 * ips, P50: int32(400 + i%500)}
			p := place(i)
			cc, country, rc, region, city := p["country_code"].(string), p["country"].(string), p["region_code"].(string), p["region"].(string), p["city"].(string)
			switch name {
			case "country":
				cc, country = countryCode(i), "Country "+countryCode(i)
				r.Place = &store.Place{CountryCode: &cc, Country: &country}
			case "region":
				r.Place = &store.Place{CountryCode: &cc, Country: &country, RegionCode: &rc, Region: &region}
			case "city", "city_asn":
				r.Place = &store.Place{CountryCode: &cc, Country: &country, RegionCode: &rc, Region: &region, City: &city}
			}
			if name == "asn" || name == "city_asn" {
				nw := network(i % max(w.Asns, 1))
				asn, label, usage := int64(nw["asn"].(int)), nw["name"].(string), nw["usage_type"].(string)
				r.Network = &store.Network{Asn: &asn, Name: &label, UsageType: &usage}
			}
			rows[name][i] = r
		}
	}
	return rows
}

// EventsSQL inserts the week's test events, newest first a minute apart from
// 2026-11-03 04:00 UTC, as events() makes them: a pulse down, its recovery
// and a latency rise, then hour releases of each network in turn.
func (w WeekShape) EventsSQL() string {
	return fmt.Sprintf(`INSERT INTO test_events(at,kind,product_id,signal,severity,data)
SELECT '2026-11-03T04:00:00Z'::timestamptz - i * interval '1 minute',
 CASE i WHEN 0 THEN 'pulse_down' WHEN 1 THEN 'pulse_recovered' WHEN 2 THEN 'latency_rise' ELSE 'estimate_hour_released' END,
 (SELECT product_id FROM products WHERE provider = 'net' || lpad((i %% %d)::text, 2, '0') LIMIT 1),
 CASE WHEN i < 3 THEN -1 ELSE 0 END, CASE WHEN i < 3 THEN 'major' ELSE 'info' END,
 CASE WHEN i < 3 THEN '{"source":"pulse","p50_ms":900,"baseline_ms":400}'::jsonb ELSE jsonb_build_object('day','2026-11-02',
  'hour',i %% 24,'formula_version',1,'unique_ips',500+i,'requests',3000+i) END
FROM generate_series(0, %d) i`, max(w.Networks, 1), w.Events-1)
}

// TargetsSQL inserts the week's browser targets into public.targets, as
// BrowserTargetsSQL does BrowserTargets.
func (w WeekShape) TargetsSQL() string {
	return targetsSQL(append(append([]BrowserTarget{}, BrowserTargets...), weekTargets...))
}
