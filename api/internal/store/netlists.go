package store

import (
	"cmp"
	"context"
	"encoding/json"
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
// holds only each list's row counts (API-MEM-1): a reader reads a dimension's
// rows from the database when it needs them, as compact rows (contract K11):
// 24 bytes a row, three counters, the p50 and dense indexes into name tables
// shared by every list a NetLists reads, so a place or ASN label is held once
// however many rows name it. The rows read last stay in a byte-bounded cache
// (rowCache). observations, challenges, blocks, p90 and p99 are not kept: no
// page shows them.

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

// netNames are the shared names as one read left them, with their lower-case
// search text. Reads only append to them, so the names read rows see never
// change while later reads run.
type netNames struct {
	places                 []*Place
	networks               []*Network
	placeText, networkText []string
}

// NetList is one network list: its id and row count per dimension. Its rows
// stay in the database (contract K2 lists are immutable: a build deletes
// lists, never changes them) and are read when a reader asks for them (Rows),
// through the row cache of the NetLists that loaded it. A list NewNetList
// makes holds its rows instead (src nil). A NetList is never changed once
// loaded.
type NetList struct {
	id    int64
	count [len(netCodes)]int
	src   *NetLists
	rows  [len(netCodes)][]netRow
	names netNames
}

// NetRows is one dimension's rows of a NetList, in stored position order,
// with the names they index.
type NetRows struct {
	rows  []netRow
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

// NetLists is the network lists the API holds between loads, by list id,
// the shared names of the rows read so far and the rows readers asked for
// last (at most DefaultRowCacheBytes unless the Lists set it). Names are kept for the process's life, as
// Lists keeps them: they are bounded by place_names and asn_label_names.
type NetLists struct {
	mu   sync.Mutex
	db   TxBeginner // reads rows outside a load
	held map[int64]*NetList
	// reading serializes row reads and guards the names they append to.
	reading sync.Mutex
	labels
	names              netNames
	placeAt, networkAt map[int32]int32 // label id -> index in names
	cache              rowCache
	// Fetched counts the list ids whose row counts were read, Reads the
	// dimensions whose rows were read from the database, for tests and logs.
	Fetched, Reads int
}

// NewNetLists is an empty cache that reads rows through db.
func NewNetLists(db TxBeginner) *NetLists {
	n := &NetLists{db: db, cache: rowCache{limit: DefaultRowCacheBytes}}
	n.reset()
	return n
}

// netEntries are the breakdown names of netCodes' dimensions, by which
// calculated_lists.entries counts a list's rows (proxy-index calculated
// list_write.py and netlists.py write it with the rows; tests/test_calc_api_head.py
// checks these names against the rows a build stores).
var netEntries = [len(netCodes)]string{"country", "region", "city", "asn", "city_asn"}

// netCountSQL is the row counts of lists $1 by breakdown name, as the
// framework stored them with the rows (calculated_lists.entries, NOT NULL
// since calculated v9): a primary-key read of one row a list.
const netCountSQL = `SELECT list_id, entries FROM public.calculated_lists WHERE list_id = ANY($1)`

// Load makes n hold exactly the lists ids, reading in tx the row counts of
// those it does not hold yet, and returns them by id. Lists not in ids are
// dropped; a failed load drops every held list.
func (n *NetLists) Load(ctx context.Context, tx pgx.Tx, ids []int64) (map[int64]*NetList, error) {
	n.mu.Lock()
	defer n.mu.Unlock()
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
		fresh[id] = &NetList{id: id, src: n}
		read = append(read, id)
	}
	n.held = kept
	if err := n.counts(ctx, tx, read, fresh); err != nil {
		n.held = nil
		return nil, err
	}
	maps.Copy(kept, fresh)
	return maps.Clone(kept), nil
}

// counts reads the row counts of ids into fresh.
func (n *NetLists) counts(ctx context.Context, tx pgx.Tx, ids []int64, fresh map[int64]*NetList) error {
	if len(ids) == 0 {
		return nil
	}
	n.Fetched += len(ids)
	var id int64
	var raw []byte
	rows, _ := tx.Query(ctx, netCountSQL, ids)
	_, err := pgx.ForEachRow(rows, []any{&id, &raw}, func() error {
		var entries map[string]int
		if err := json.Unmarshal(raw, &entries); err != nil {
			return fmt.Errorf("list %d entries: %w", id, err)
		}
		for d, name := range netEntries {
			n, ok := entries[name]
			if !ok {
				return fmt.Errorf("list %d entries have no %s count", id, name)
			}
			fresh[id].count[d] = n
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("network list counts: %w", err)
	}
	return nil
}

// reset empties n's names: the first read, and every read after a failed one.
func (n *NetLists) reset() {
	n.labels = labels{places: map[int32]*Place{}, networks: map[int32]*Network{}}
	n.names, n.placeAt, n.networkAt = netNames{}, map[int32]int32{}, map[int32]int32{}
}

// rows is dimension d of l: the cached rows, else read in tx (nil: a
// read-only transaction of its own). A read that does not give the row count
// l was loaded with fails: the list was deleted since.
func (n *NetLists) rows(ctx context.Context, tx pgx.Tx, l *NetList, d int) (*NetRows, error) {
	key := rowKey{l.id, d}
	if r := n.cache.get(key); r != nil {
		return r, nil
	}
	n.reading.Lock()
	defer n.reading.Unlock()
	if r := n.cache.get(key); r != nil {
		return r, nil
	}
	if tx == nil {
		var err error
		if tx, err = n.db.BeginTx(ctx, pgx.TxOptions{AccessMode: pgx.ReadOnly}); err != nil {
			return nil, fmt.Errorf("network list %d: %w", l.id, err)
		}
		defer tx.Rollback(ctx) //nolint:errcheck // read-only
	}
	n.Reads++
	r, err := n.read(ctx, tx, l, d)
	if err != nil {
		n.reset()
		return nil, fmt.Errorf("network list %d: %w", l.id, err)
	}
	n.cache.add(key, r)
	return r, nil
}

// read reads dimension d of l, then the names its rows created.
func (n *NetLists) read(ctx context.Context, tx pgx.Tx, l *NetList, d int) (*NetRows, error) {
	n.missing = nil
	from := [2]int{len(n.names.places), len(n.names.networks)}
	rows := make([]netRow, 0, l.count[d])
	err := scanList(ctx, tx, true, 0, []int64{l.id}, netCodes[d:d+1], func(s *scannedRow) error {
		r, err := n.compact(s)
		rows = append(rows, r)
		return err
	})
	if err == nil {
		err = n.readLabels(ctx, tx)
	}
	if err == nil && len(rows) != l.count[d] {
		err = fmt.Errorf("%d %s rows, %d when it was loaded: the list was deleted", len(rows), netCodes[d:d+1], l.count[d])
	}
	if err != nil {
		return nil, err
	}
	n.fillTexts(from)
	return &NetRows{rows: rows, names: n.names}, nil
}

// compact is s as a compact row, its names appended when new.
func (n *NetLists) compact(s *scannedRow) (netRow, error) {
	r := netRow{p50: s.P50, place: -1, network: -1}
	var err error
	for _, c := range []struct {
		to   *uint32
		from int64
	}{{&r.ips, s.Ips}, {&r.completed, s.Completed}, {&r.successes, s.Successes}} {
		if *c.to, err = counter(c.from); err != nil {
			return r, err
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
	return r, nil
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

// dimIndex is the index in netCodes of a payload breakdown name; -1 when a
// network list has none.
func dimIndex(dimension string) int {
	code, ok := dimensionCodes[dimension]
	if !ok {
		return -1
	}
	return strings.IndexByte(netCodes, code)
}

// Len is the number of rows of a dimension.
func (l *NetList) Len(dimension string) int {
	d := dimIndex(dimension)
	if l == nil || d < 0 {
		return 0
	}
	return l.count[d]
}

// Rows is a dimension's rows (none when the list has no such dimension):
// from the row cache, else read from the database in a read-only transaction
// of their own.
func (l *NetList) Rows(ctx context.Context, dimension string) (*NetRows, error) {
	return l.rowsIn(ctx, nil, dimension)
}

// rowsIn is Rows, read in tx when not cached (nil: a transaction of its own).
func (l *NetList) rowsIn(ctx context.Context, tx pgx.Tx, dimension string) (*NetRows, error) {
	d := dimIndex(dimension)
	switch {
	case l == nil || d < 0:
		return &NetRows{}, nil
	case l.src == nil:
		return &NetRows{rows: l.rows[d], names: l.names}, nil
	case l.count[d] == 0:
		return &NetRows{}, nil
	}
	return l.src.rows(ctx, tx, l, d)
}

// Len is the number of rows.
func (l *NetRows) Len() int { return len(l.rows) }

// Row is row i, in stored position order.
func (l *NetRows) Row(i int) NetRow {
	r := l.rows[i]
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

// ByIps is the row indexes, most IPs first; ties keep their stored position
// order.
func (l *NetRows) ByIps() []int32 {
	rows := l.rows
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
func (l *NetRows) Match(q string, order []int32) []int32 {
	rows, words := l.rows, strings.Fields(strings.ToLower(q))
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

func (l *NetRows) holds(r *netRow, words []string) bool {
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

// Totals are the summed IPs and distinct countries, cities and ASNs of the
// rows at indexes.
func (l *NetRows) Totals(indexes []int32) NetTotals {
	rows := l.rows
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
