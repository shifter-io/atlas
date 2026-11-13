package testscale

import (
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strings"

	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// NetworkShape is a payload v4 build (atlas calculated contract K9):
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
// with one product group per network (NetworkHead), the lists held in memory
// as store reads them.
func Networks(row *store.SnapshotRow, requested int) ([]byte, map[int64]*store.NetList, error) {
	lists := map[int64]*store.NetList{}
	head, err := NetworkHead(row, requested, func(_ NetWindow, rows map[string][]store.NetRow) (int64, map[string]int, error) {
		id := int64(len(lists) + 1)
		l, err := store.NewNetList(rows)
		lists[id] = l
		return id, RowCounts(rows), err
	})
	return head, lists, err
}

// NetWindow is a network window whose list a ListWriter stores: its
// network's provider and product name, and the window.
type NetWindow struct {
	Provider, Product, Label string
	Hours                    int64
}

// ListWriter stores the list of a network window, its rows by payload
// breakdown name, and returns the list id the head names and its row count
// by breakdown name.
type ListWriter func(w NetWindow, rows map[string][]store.NetRow) (int64, map[string]int, error)

// netNames are the payload breakdown names a network list has rows of.
var netNames = []string{"country", "region", "city", "asn", "city_asn"}

// RowCounts is the row count of each breakdown name a network list has.
func RowCounts(rows map[string][]store.NetRow) map[string]int {
	out := map[string]int{}
	for _, name := range netNames {
		out[name] = len(rows[name])
	}
	return out
}

// NetworkHead is the v4 head of a snapshot before v4 (whole, with its
// product groups) with one product group per network: each group is its
// network's summary (contract K9: the keys a network window publishes, its
// peers made from the snapshot's overlap rows) and each window's list,
// stored by write, holds the rows the build reads of the group (its typed
// rows when complete, else its breakdowns or, without those, its pool lists).
// requested > 0 gives every network that many requested countries. The head
// is as store reads it: without parts, runs, overlap rows or per-cohort fleet
// rows (it keeps the every-group "*" and "*:<kind>" ones).
func NetworkHead(row *store.SnapshotRow, requested int, write ListWriter) ([]byte, error) {
	var doc map[string]any
	var typed snap.Snapshot
	err := json.Unmarshal(row.Full(), &doc)
	if err == nil {
		err = json.Unmarshal(row.Full(), &typed)
	}
	if err != nil {
		return nil, err
	}
	networks := []any{}
	for i, g := range asList(doc["products"]) {
		entry, err := networkEntry(g.(map[string]any), &typed.Products[i], &typed, row, requested, write)
		if err != nil {
			return nil, fmt.Errorf("group %d: %w", i, err)
		}
		networks = append(networks, entry)
	}
	fleet := []any{}
	for _, f := range asList(doc["fleet"]) {
		if c, _ := f.(map[string]any)["cohort"].(string); strings.HasPrefix(c, snap.AllCohorts) {
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
	return json.Marshal(doc)
}

func asList(v any) []any {
	l, _ := v.([]any)
	return l
}

// unpublished are the group window keys a network window does not publish.
var unpublished = []string{"benchmark", "previous_period", "census_overlap", "latency", "evidence_sources", "unavailable", "roles",
	"breakdowns", "breakdown_entries", "lists", "ip_samples"}

// networkEntry turns group g (p typed) into its network's entry, its
// windows' lists stored by write.
func networkEntry(g map[string]any, p *snap.Product, s *snap.Snapshot, row *store.SnapshotRow, requested int, write ListWriter) (map[string]any, error) {
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
		w := p.Windows[label]
		id, n, err := write(NetWindow{p.Provider, p.Product, label, int64(w.Period.Hours)}, windowRows(row, p, w))
		if err != nil {
			return nil, fmt.Errorf("window %s: %w", label, err)
		}
		ids[label], entries[label], peerRows[label] = id, n, peers(s, p, label)
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
	g["id"] = NetworkID(g["provider"], g["kind"])
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

// NetworkID is the id of the v4 network of provider's product kind.
func NetworkID(provider, kind any) string {
	return fmt.Sprintf("%s:%s:network", provider, kind)
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

// windowRows is the network list rows of a group window by payload
// breakdown name: country and City x ASN rows from the breakdowns, region,
// city and ASN rows from the typed rows when they are whole; a window
// without a breakdown (a v1 snapshot's) has its pool list's country or ASN
// rows instead, IPs only.
func windowRows(row *store.SnapshotRow, p *snap.Product, w snap.Window) map[string][]store.NetRow {
	typed := row.Places[store.PlaceWindow{Product: int64(p.ProductID), Cohort: p.Cohort, Hours: int64(w.Period.Hours)}]
	rows := map[string][]store.NetRow{}
	for _, name := range netNames {
		list := typed.Rows(name)
		_, recorded := w.Breakdowns[name]
		switch {
		case name != "country" && name != "city_asn" && list != nil && len(list) == w.BreakdownEntries[name]:
			for _, r := range list {
				rows[name] = append(rows[name], store.NetRow{Place: r.Place, Network: r.Network, Ips: r.Ips, Completed: r.Completed,
					Successes: r.Successes, P50: r.P50})
			}
		case !recorded:
			rows[name] = poolRows(name, w.Pool)
		default:
			for _, r := range w.Breakdowns[name] {
				rows[name] = append(rows[name], netRow(name, r))
			}
		}
	}
	return rows
}

// poolRows is pool list rows of breakdown name (country or asn) as network
// list rows, without request counters.
func poolRows(name string, pool snap.Pool) []store.NetRow {
	var out []store.NetRow
	switch name {
	case "country":
		for _, c := range pool.CountriesBreakdown {
			out = append(out, store.NetRow{Place: &store.Place{CountryCode: &c.CountryCode, Country: &c.Country}, Ips: count(c.Ips),
				Completed: -1, Successes: -1, P50: -1})
		}
	case "asn":
		for _, a := range pool.AsnsBreakdown {
			n := &store.Network{Name: &a.Name, UsageType: &a.UsageType}
			if a.Asn.OK() {
				asn := int64(a.Asn)
				n.Asn = &asn
			}
			out = append(out, store.NetRow{Network: n, Ips: count(a.Ips), Completed: -1, Successes: -1, P50: -1})
		}
	}
	return out
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
