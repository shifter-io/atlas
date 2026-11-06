package testscale

import (
	"encoding/json"
	"fmt"
	"math"
	"sort"

	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// NetworkShape is a payload v4 build (proxy-index calculated contract K9):
// Shape with one product group per network, each published as its network's
// summary over Requested requested countries (0: its group's own).
type NetworkShape struct {
	Shape
	Requested int
}

// QA1Networks is the QA-1 build as payload v4: its 18 networks, each one
// summary over its 40 requested countries, with network lists (contract K10)
// of every exit country, 2,000 City x ASN rows and the tails per window.
var QA1Networks = NetworkShape{Shape: Shape{Networks: 18, Countries: 1, Exits: 240, Tail: 200, Unmeasured: 3, Events: 559,
	Browser: true, CityAsn: 2000}, Requested: 40}

// NetworkInputs is Inputs(shape.Shape) published as payload v4: its snapshot
// is the head and the network lists as store reads them, Full and Places set
// from them (store.SnapshotRow.SetNetworks).
func NetworkInputs(shape NetworkShape) (*store.Inputs, error) {
	in, err := Inputs(shape.Shape)
	if err != nil {
		return nil, err
	}
	head, lists, err := Networks(in.Snapshot, shape.Requested)
	if err != nil {
		return nil, err
	}
	in.Snapshot = &store.SnapshotRow{BuildID: in.Snapshot.BuildID, ContentHash: in.Snapshot.ContentHash, Payload: head}
	return in, in.Snapshot.SetNetworks(lists)
}

// Networks is the v4 head and network lists, by list id, of a v3 snapshot
// with one product group per network: each group is its network's summary
// (contract K9: the keys a network window publishes, its peers made from the
// snapshot's overlap rows) and each window's list holds the rows the build
// reads of the group (its typed rows when complete, else its breakdowns).
// requested > 0 gives every network that many requested countries. The head
// is as store reads it: without parts, runs, overlap rows or per-cohort fleet
// rows.
func Networks(row *store.SnapshotRow, requested int) ([]byte, map[int64]*store.NetList, error) {
	var doc map[string]any
	var typed snap.Snapshot
	err := json.Unmarshal(row.Full(), &doc)
	if err == nil {
		err = json.Unmarshal(row.Full(), &typed)
	}
	if err != nil {
		return nil, nil, err
	}
	lists := map[int64]*store.NetList{}
	networks := []any{}
	for i, g := range doc["products"].([]any) {
		entry, err := networkEntry(g.(map[string]any), &typed.Products[i], &typed, row, requested, lists)
		if err != nil {
			return nil, nil, fmt.Errorf("group %d: %w", i, err)
		}
		networks = append(networks, entry)
	}
	fleet := []any{}
	for _, f := range asList(doc["fleet"]) {
		if f.(map[string]any)["cohort"] == snap.AllCohorts {
			fleet = append(fleet, f)
		}
	}
	for _, k := range []string{"products", "runs", "overlap", "parts"} {
		delete(doc, k)
	}
	if _, ok := doc["fleet"]; ok {
		doc["fleet"] = fleet
	}
	doc["schema_version"], doc["networks"] = 4, networks
	head, err := json.Marshal(doc)
	return head, lists, err
}

func asList(v any) []any {
	l, _ := v.([]any)
	return l
}

// unpublished are the group window keys a network window does not publish.
var unpublished = []string{"benchmark", "previous_period", "census_overlap", "latency", "evidence_sources", "unavailable", "roles",
	"breakdowns", "breakdown_entries", "lists", "ip_samples"}

// networkEntry turns group g (p typed) into its network's entry, adding its
// windows' lists to lists.
func networkEntry(g map[string]any, p *snap.Product, s *snap.Snapshot, row *store.SnapshotRow, requested int, lists map[int64]*store.NetList) (map[string]any, error) {
	countries, groups := []string{}, 1
	if cc := p.Criteria.Country; cc != nil {
		countries = []string{upper(*cc)}
	}
	if requested > 0 {
		countries, groups = []string{}, requested
		for c := range requested {
			countries = append(countries, countryCode(c))
		}
	}
	ids, entries, peerRows := map[string]int64{}, map[string]map[string]int{}, map[string][]snap.OverlapPeer{}
	for label := range g["windows"].(map[string]any) {
		l, n, err := windowList(row, p, p.Windows[label])
		if err != nil {
			return nil, fmt.Errorf("window %s: %w", label, err)
		}
		ids[label] = int64(len(lists) + 1)
		lists[ids[label]], entries[label], peerRows[label] = l, n, peers(s, p, label)
	}
	return NetworkEntry(g, countries, groups, ids, entries, peerRows), nil
}

// NetworkEntry is v3 product group g (edited in place) as its network's v4
// entry (contract K9) when the network is that group: its id and cohort, its
// criteria (requested countries, groups), and each window with only the keys
// a network window publishes, its list (lists[label], with entries[label]
// rows by breakdown name) and its overlap peers.
func NetworkEntry(g map[string]any, requested []string, groups int, lists map[string]int64, entries map[string]map[string]int,
	peers map[string][]snap.OverlapPeer) map[string]any {
	g["id"] = fmt.Sprintf("%s:%s:network", g["provider"], g["kind"])
	g["cohort"] = store.NetworkCohort
	g["criteria"] = map[string]any{"country": nil, "requested_countries": requested, "groups": groups}
	delete(g, "scoring")
	for label, raw := range g["windows"].(map[string]any) {
		w := raw.(map[string]any)
		for _, k := range unpublished {
			delete(w, k)
		}
		if pool, ok := w["pool"].(map[string]any); ok {
			for _, k := range []string{"geo", "countries_breakdown", "asns_breakdown"} {
				delete(pool, k)
			}
		}
		if scores, ok := w["scores"].(map[string]any); ok {
			w["scores"] = map[string]any{"balanced": scores["balanced"]}
		}
		overlap, _ := w["overlap"].(map[string]any)
		if overlap == nil {
			overlap = map[string]any{}
		}
		rows := peers[label]
		if rows == nil {
			rows = []snap.OverlapPeer{}
		}
		overlap["peers"] = rows
		w["overlap"], w["lists"] = overlap, map[string]any{"id": lists[label], "entries": entries[label]}
	}
	return g
}

func upper(s string) string {
	b := []byte(s)
	for i, c := range b {
		if 'a' <= c && c <= 'z' {
			b[i] = c - 'a' + 'A'
		}
	}
	return string(b)
}

// peers is the group's overlap rows of window label as its network's
// overlap.peers, most shared IPs first.
func peers(s *snap.Snapshot, p *snap.Product, label string) []snap.OverlapPeer {
	providerOf := map[snap.Num]string{}
	for _, q := range s.Products {
		providerOf[q.ProductID] = q.Provider
	}
	out := []snap.OverlapPeer{}
	for _, r := range s.Overlap {
		if r.Window != label || r.Cohort != p.Cohort || !(r.SharedIps > 0) {
			continue
		}
		switch p.ProductID {
		case r.ProductA:
			out = append(out, snap.OverlapPeer{Provider: providerOf[r.ProductB], ProductID: r.ProductB, SharedIps: r.SharedIps, IpsOurs: r.IpsA, IpsTheirs: r.IpsB})
		case r.ProductB:
			out = append(out, snap.OverlapPeer{Provider: providerOf[r.ProductA], ProductID: r.ProductA, SharedIps: r.SharedIps, IpsOurs: r.IpsB, IpsTheirs: r.IpsA})
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].SharedIps > out[j].SharedIps })
	return out
}

// windowList is the network list of a group window and its row counts by
// payload breakdown name: country and City x ASN rows from the breakdowns,
// region, city and ASN rows from the typed rows when they are whole.
func windowList(row *store.SnapshotRow, p *snap.Product, w snap.Window) (*store.NetList, map[string]int, error) {
	typed := row.Places[store.PlaceWindow{Product: int64(p.ProductID), Cohort: p.Cohort, Hours: int64(w.Period.Hours)}]
	rows, entries := map[string][]store.NetRow{}, map[string]int{}
	for _, name := range []string{"country", "region", "city", "asn", "city_asn"} {
		list := typed.Rows(name)
		if name != "country" && name != "city_asn" && list != nil && len(list) == w.BreakdownEntries[name] {
			for _, r := range list {
				rows[name] = append(rows[name], store.NetRow{Place: r.Place, Network: r.Network, Ips: r.Ips, Completed: r.Completed,
					Successes: r.Successes, P50: r.P50})
			}
		} else {
			for _, r := range w.Breakdowns[name] {
				rows[name] = append(rows[name], netRow(name, r))
			}
		}
		entries[name] = len(rows[name])
	}
	l, err := store.NewNetList(rows)
	return l, entries, err
}

// netRow is a breakdown row of name as a network list row.
func netRow(name string, r snap.BreakdownRow) store.NetRow {
	out := store.NetRow{Ips: count(r.Ips), Completed: count(r.Completed), Successes: count(r.Successes), P50: int32(count(r.P50))}
	if name != "asn" {
		out.Place = &store.Place{CountryCode: r.CountryCode, Country: r.Country}
		if name != "country" {
			out.Place.RegionCode, out.Place.Region = r.RegionCode, r.Region
		}
		if name == "city" || name == "city_asn" {
			out.Place.City = r.City
		}
	}
	if name == "asn" || name == "city_asn" {
		out.Network = &store.Network{Name: r.Name, UsageType: r.UsageType}
		if r.Asn.OK() {
			asn := int64(r.Asn)
			out.Network.Asn = &asn
		}
	}
	return out
}

// count is a counter as a list row has it: -1 for null.
func count(v snap.Num) int64 {
	if !v.OK() {
		return -1
	}
	return int64(math.Round(v.F()))
}
