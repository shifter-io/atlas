package store

import (
	"cmp"
	"context"
	"fmt"
	"maps"
	"math"
	"slices"
	"strconv"
	"strings"
	"sync"

	"github.com/jackc/pgx/v5"
)

// A network list (proxy-index calculated contract K10) is one network
// window's list in the K2 tables, with dimensions c, r, g, a and y. The API
// holds them whole as compact rows (contract K11): 24 bytes a row, three
// counters, the p50 and dense indexes into name tables shared by every list a
// NetLists holds, so a place or ASN label is held once however many rows name
// it. observations, challenges, blocks, p90 and p99 are not kept: no page
// shows them.

// netCodes are a network list's dimension codes: there is no country x ASN.
const netCodes = "crgay"

// null32 is a NULL counter of a compact row. A counter at or above it fails
// the load: it does not fit the row.
const null32 = math.MaxUint32

// netRow is one compact row: ips, completed and successes (null32 when NULL),
// p50 (-1 when NULL), and the dense indexes of its place and network names
// (-1 when none). Its position is its index in the dimension's rows.
type netRow struct {
	ips, completed, successes uint32
	p50, place, network       int32
}

// netNames are the shared names as one load left them, with their lower-case
// search text. Loads only append to them, so the names a loaded list sees
// never change while later loads run.
type netNames struct {
	places                 []*Place
	networks               []*Network
	placeText, networkText []string
}

// NetList is one network list, its rows per dimension in stored position
// order. A NetList is never changed once loaded.
type NetList struct {
	rows  [len(netCodes)][]netRow
	names netNames
}

// NetRow is one row of a NetList as readers see it: Place is nil for an ASN
// row, Network for a place row; a NULL counter or p50 is -1.
type NetRow struct {
	Place                     *Place
	Network                   *Network
	Ips, Completed, Successes int64
	P50                       int32
}

// NetTotals are a set of rows' summed IPs and their distinct countries
// (country codes), cities (city places) and ASNs (numbers).
type NetTotals struct {
	Ips                     int64
	Countries, Cities, Asns int
}

// NetLists is the network lists the API holds between loads, by list id, and
// their shared names. Names are kept for the process's life, as Lists keeps
// them: they are bounded by place_names and asn_label_names.
type NetLists struct {
	mu   sync.Mutex
	held map[int64]*NetList
	labels
	names              netNames
	placeAt, networkAt map[int32]int32 // label id -> index in names
	// Fetched counts the list ids read from the database, for tests and logs.
	Fetched int
}

// NewNetLists is an empty cache.
func NewNetLists() *NetLists { return &NetLists{} }

// Load makes n hold exactly the lists ids, reading in tx only those it does
// not hold yet, and returns them by id. Lists not in ids are dropped before
// the new ones are read. A failed load drops every held list at once, and the
// next load starts with empty names, so nothing half-read is kept.
func (n *NetLists) Load(ctx context.Context, tx pgx.Tx, ids []int64) (map[int64]*NetList, error) {
	n.mu.Lock()
	defer n.mu.Unlock()
	if n.held == nil {
		n.reset()
	}
	kept, fresh := map[int64]*NetList{}, map[int64]*NetList{}
	var read []int64
	for _, id := range ids {
		if kept[id] != nil || fresh[id] != nil {
			continue
		}
		if l := n.held[id]; l != nil {
			kept[id] = l
			continue
		}
		fresh[id] = &NetList{}
		read = append(read, id)
	}
	n.held = kept
	if err := n.read(ctx, tx, read, fresh); err != nil {
		n.held, n.missing = nil, nil
		return nil, err
	}
	maps.Copy(kept, fresh)
	return maps.Clone(kept), nil
}

// reset empties n's names: the first load, and every load after a failed one.
func (n *NetLists) reset() {
	n.labels = labels{places: map[int32]*Place{}, networks: map[int32]*Network{}}
	n.names, n.placeAt, n.networkAt = netNames{}, map[int32]int32{}, map[int32]int32{}
}

// read reads the rows of ids into fresh, then the names they created.
func (n *NetLists) read(ctx context.Context, tx pgx.Tx, ids []int64, fresh map[int64]*NetList) error {
	if len(ids) == 0 {
		return nil
	}
	n.Fetched += len(ids)
	n.missing = nil
	from := [2]int{len(n.names.places), len(n.names.networks)}
	err := scanList(ctx, tx, true, 0, ids, netCodes, func(s *scannedRow) error { return n.add(fresh[s.id], s) })
	if err == nil {
		err = n.readLabels(ctx, tx)
	}
	if err != nil {
		return err
	}
	n.fillTexts(from)
	for _, l := range fresh {
		l.finish(n.names)
	}
	return nil
}

// add appends s to l as a compact row.
func (n *NetLists) add(l *NetList, s *scannedRow) error {
	r := netRow{p50: s.P50, place: -1, network: -1}
	var err error
	for _, c := range []struct {
		to   *uint32
		from int64
	}{{&r.ips, s.Ips}, {&r.completed, s.Completed}, {&r.successes, s.Successes}} {
		if *c.to, err = counter(c.from); err != nil {
			return fmt.Errorf("list %d: %w", s.id, err)
		}
	}
	if s.place >= 0 {
		r.place = index(n.placeAt, s.place, func() {
			n.names.places = append(n.names.places, n.place(s.place))
			n.names.placeText = append(n.names.placeText, "")
		})
	}
	if s.network >= 0 {
		r.network = index(n.networkAt, s.network, func() {
			n.names.networks = append(n.names.networks, n.network(s.network))
			n.names.networkText = append(n.names.networkText, "")
		})
	}
	d := strings.IndexByte(netCodes, s.code)
	l.rows[d] = append(l.rows[d], r)
	return nil
}

// counter is v (-1 for NULL) as a compact counter.
func counter(v int64) (uint32, error) {
	if v < 0 {
		return null32, nil
	}
	if v >= null32 {
		return 0, fmt.Errorf("counter %d does not fit a compact row", v)
	}
	return uint32(v), nil
}

// index is the dense index of label id in at, appended by add when new.
func index(at map[int32]int32, id int32, add func()) int32 {
	i, ok := at[id]
	if !ok {
		i = int32(len(at))
		at[id] = i
		add()
	}
	return i
}

// fillTexts sets the search text of the names appended since from, once
// readLabels has filled them: a place's country code, country, region and
// city; a network's "as<n>" (which holds "<n>") and name; all lower case.
func (n *NetLists) fillTexts(from [2]int) {
	for i := from[0]; i < len(n.names.places); i++ {
		p := n.names.places[i]
		n.names.placeText[i] = searchText(p.CountryCode, p.Country, p.Region, p.City)
	}
	for i := from[1]; i < len(n.names.networks); i++ {
		w, as := n.names.networks[i], ""
		if w.Asn != nil {
			as = "as" + strconv.FormatInt(*w.Asn, 10)
		}
		n.names.networkText[i] = searchText(&as, w.Name)
	}
}

func searchText(parts ...*string) string {
	var b strings.Builder
	for _, p := range parts {
		if p != nil {
			b.WriteString(strings.ToLower(*p))
			b.WriteByte(' ')
		}
	}
	return b.String()
}

// finish trims l's rows to their length and gives l the names as they are.
func (l *NetList) finish(names netNames) {
	for d, rows := range l.rows {
		if cap(rows) > len(rows) {
			l.rows[d] = append(make([]netRow, 0, len(rows)), rows...)
		}
	}
	l.names = names
}

// dim is the rows of a payload breakdown name; nil when the list has none.
func (l *NetList) dim(dimension string) []netRow {
	d := strings.IndexByte(netCodes, dimensionCodes[dimension])
	if l == nil || d < 0 {
		return nil
	}
	return l.rows[d]
}

// Len is the number of rows of a dimension.
func (l *NetList) Len(dimension string) int { return len(l.dim(dimension)) }

// Row is a dimension's row i, in stored position order.
func (l *NetList) Row(dimension string, i int) NetRow {
	r := l.dim(dimension)[i]
	v := NetRow{Ips: wide(r.ips), Completed: wide(r.completed), Successes: wide(r.successes), P50: r.p50}
	if r.place >= 0 {
		v.Place = l.names.places[r.place]
	}
	if r.network >= 0 {
		v.Network = l.names.networks[r.network]
	}
	return v
}

func wide(v uint32) int64 {
	if v == null32 {
		return -1
	}
	return int64(v)
}

// ByIps is a dimension's row indexes, most IPs first; ties keep their stored
// position order.
func (l *NetList) ByIps(dimension string) []int32 {
	rows := l.dim(dimension)
	order := make([]int32, len(rows))
	for i := range order {
		order[i] = int32(i)
	}
	slices.SortStableFunc(order, func(a, b int32) int { return cmp.Compare(rows[b].ips, rows[a].ips) })
	return order
}

// Match is the indexes of order (nil: every row in position order) whose row
// holds every word of q, case-insensitive, in its country code, country,
// region, city, "as<n>", "<n>" or ASN name. A q without words matches every row.
func (l *NetList) Match(dimension, q string, order []int32) []int32 {
	rows, words := l.dim(dimension), strings.Fields(strings.ToLower(q))
	if order == nil {
		order = make([]int32, len(rows))
		for i := range order {
			order[i] = int32(i)
		}
	}
	out := []int32{}
	for _, i := range order {
		if l.holds(&rows[i], words) {
			out = append(out, i)
		}
	}
	return out
}

func (l *NetList) holds(r *netRow, words []string) bool {
	place, network := "", ""
	if r.place >= 0 {
		place = l.names.placeText[r.place]
	}
	if r.network >= 0 {
		network = l.names.networkText[r.network]
	}
	for _, w := range words {
		if !strings.Contains(place, w) && !strings.Contains(network, w) {
			return false
		}
	}
	return true
}

// Totals are the summed IPs and distinct countries, cities and ASNs of a
// dimension's rows at indexes.
func (l *NetList) Totals(dimension string, indexes []int32) NetTotals {
	rows := l.dim(dimension)
	countries, cities, asns := map[string]bool{}, map[int32]bool{}, map[int64]bool{}
	var t NetTotals
	for _, i := range indexes {
		r := &rows[i]
		t.Ips += max(wide(r.ips), 0)
		if r.place >= 0 {
			p := l.names.places[r.place]
			if p.CountryCode != nil {
				countries[*p.CountryCode] = true
			}
			if p.City != nil {
				cities[r.place] = true
			}
		}
		if r.network >= 0 {
			if a := l.names.networks[r.network].Asn; a != nil {
				asns[*a] = true
			}
		}
	}
	t.Countries, t.Cities, t.Asns = len(countries), len(cities), len(asns)
	return t
}
