package store

import (
	"context"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"sync"
	"unicode"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

// A published window's lists are its typed rows (proxy-index calculated
// contract K2, calculated v9): calculated_list_rows keyed by list_id, which
// calculated_windows.list_id and, from payload v3, the window's "lists.id"
// name. Builds share unchanged lists, so the API keeps the rows of every list
// a current window references (Lists) and fetches only the ids it does not
// hold. Before K2 the rows are the build's calculated_breakdowns partition,
// keyed by result_id; they are read the same way, the result id standing for
// the list id.

// dimensionCodes are the typed rows' codes of the payload's breakdown names.
var dimensionCodes = map[string]byte{"country": 'c', "region": 'r', "city": 'g', "asn": 'a', "country_asn": 'x', "city_asn": 'y'}

// fullCodes are read whole for every list; cityAsn only for the groups that
// need it (readLists).
const (
	fullCodes = "crgax"
	cityAsn   = "y"
)

// Place is one place_names row, Network one asn_label_names row.
type (
	Place   struct{ CountryCode, Country, RegionCode, Region, City *string }
	Network struct {
		Asn             *int64
		Name, UsageType *string
	}
)

// ListRow is one typed list row. Place is nil for an ASN row, Network for a
// place row. A NULL counter or percentile is -1.
type ListRow struct {
	Place                                                       *Place
	Network                                                     *Network
	Ips, Observations, Completed, Successes, Challenges, Blocks int64
	P50, P90, P99                                               int32
	position                                                    int32
}

// List is one list's rows per dimension code, in position order. A List is
// never changed once loaded: a new load replaces it.
type List struct{ rows map[byte][]ListRow }

// NewList is a list of the rows given per payload breakdown name, in order.
func NewList(rows map[string][]ListRow) *List {
	l := &List{rows: map[byte][]ListRow{}}
	for name, r := range rows {
		l.rows[dimensionCodes[name]] = r
	}
	return l
}

// Rows is the list's rows of a payload breakdown name; nil when not held.
func (l *List) Rows(name string) []ListRow {
	if l == nil {
		return nil
	}
	return l.rows[dimensionCodes[name]]
}

// Lists is the list rows the API holds between loads, by list id. LoadLists
// keeps only what the build it read references. Place and network names are
// kept for the process's life: they are bounded by place_names and
// asn_label_names.
type Lists struct {
	mu   sync.Mutex
	k2   bool // ids are list ids (else result ids)
	held map[int64]*List
	labels
	// Fetched counts the list ids read from the database (each once per
	// fetch), for tests and logs.
	Fetched int
	// net holds the network lists of a v4 build (network.go).
	net *NetLists
}

// labels are the place and network names a list cache holds, shared by its
// rows, and the labels the current read created, not read yet: place ids >= 0,
// network ids as -1-id (readLabels).
type labels struct {
	places   map[int32]*Place
	networks map[int32]*Network
	missing  []int32
}

// NewLists is an empty cache.
func NewLists() *Lists { return &Lists{} }

// listSQL reads the rows of ids ($1) and dimension codes ($2): from the K2
// table, or from one build's calculated_breakdowns partition ($3). Both
// filter on the stored code and use the primary key; JSON is built in Go.
const listSQL = `SELECT %[1]s, dimension::text, place_id, asn_label_id, ips, observations,
 completed, successes, challenges, blocks, p50, p90, p99 FROM public.%[2]s
 WHERE %[1]s = ANY($1) AND dimension = ANY(($2::text[])::"char"[])%[3]s
 ORDER BY %[1]s, dimension, position`

func listQuery(k2 bool) string {
	if k2 {
		return fmt.Sprintf(listSQL, "list_id", "calculated_list_rows", "")
	}
	return fmt.Sprintf(listSQL, "result_id", "calculated_breakdowns", " AND build_id = $3")
}

// fetch makes the cache hold, for every id, fullCodes plus cityAsn for the
// ids in withCityAsn, and nothing else. Every other id, and the city x ASN
// rows of ids not in withCityAsn, are dropped before the new rows are read,
// so a rebuild never holds two builds' lists at once. A failed fetch empties
// the cache, its names and its pending labels, so nothing half-read is kept.
func (c *Lists) fetch(ctx context.Context, tx pgx.Tx, k2 bool, build int64, ids []int64, withCityAsn map[int64]bool) (map[int64]*List, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.held == nil || c.k2 != k2 {
		c.k2, c.held, c.places, c.networks = k2, map[int64]*List{}, map[int32]*Place{}, map[int32]*Network{}
	}
	c.missing = nil
	kept := make(map[int64]*List, len(ids))
	byCodes := map[string][]int64{}
	for _, id := range ids {
		codes, l := "", c.held[id]
		if l == nil {
			codes = fullCodes
		} else {
			kept[id] = &List{rows: map[byte][]ListRow{}}
			for code, r := range l.rows {
				if code != cityAsn[0] || withCityAsn[id] {
					kept[id].rows[code] = r
				}
			}
		}
		if has := kept[id] != nil && kept[id].rows[cityAsn[0]] != nil; withCityAsn[id] && !has {
			codes += cityAsn
		}
		if codes != "" {
			byCodes[codes] = append(byCodes[codes], id)
			c.Fetched++
		}
	}
	c.held = kept
	fresh := map[int64]*List{}
	err := c.readAll(ctx, tx, k2, build, byCodes, fresh)
	if err != nil {
		c.held, c.missing = nil, nil
		return nil, err
	}
	for _, id := range ids {
		l := kept[id]
		if l == nil {
			l = &List{rows: map[byte][]ListRow{}}
			kept[id] = l
		}
		if f := fresh[id]; f != nil {
			for code, r := range f.rows {
				l.rows[code] = r
			}
		}
	}
	return kept, nil
}

// readAll reads every code group's rows into fresh, then their labels.
func (c *Lists) readAll(ctx context.Context, tx pgx.Tx, k2 bool, build int64, byCodes map[string][]int64, fresh map[int64]*List) error {
	for codes, part := range byCodes {
		if err := c.read(ctx, tx, k2, build, part, codes, fresh); err != nil {
			return err
		}
	}
	return c.readLabels(ctx, tx)
}

// read fetches the codes' rows of ids into fresh (in position order, as the
// query returns them), with an empty list for each code an id has no row of.
func (c *Lists) read(ctx context.Context, tx pgx.Tx, k2 bool, build int64, ids []int64, codes string, fresh map[int64]*List) error {
	for _, id := range ids {
		if fresh[id] == nil {
			fresh[id] = &List{rows: map[byte][]ListRow{}}
		}
		for i := range len(codes) {
			fresh[id].rows[codes[i]] = []ListRow{}
		}
	}
	return scanList(ctx, tx, k2, build, ids, codes, func(s *scannedRow) error {
		r := s.ListRow
		if s.place >= 0 {
			r.Place = c.place(s.place)
		}
		if s.network >= 0 {
			r.Network = c.network(s.network)
		}
		l := fresh[s.id]
		l.rows[s.code] = append(l.rows[s.code], r)
		return nil
	})
}

// scannedRow is one row as listQuery returns it: its list (or result) id, its
// dimension code, its place and network label ids (-1 when NULL) and its
// counters (Place and Network nil).
type scannedRow struct {
	id             int64
	code           byte
	place, network int32
	ListRow
}

// scanList runs listQuery for the codes' rows of ids and calls each with every
// row, in the query's order. The row is reused: each copies what it keeps.
func scanList(ctx context.Context, tx pgx.Tx, k2 bool, build int64, ids []int64, codes string, each func(*scannedRow) error) error {
	args := []any{ids, strings.Split(codes, "")}
	if !k2 {
		args = append(args, build)
	}
	var s scannedRow
	var code string
	var place, network, p50, p90, p99 pgtype.Int4
	var completed, successes, challenges, blocks pgtype.Int8
	rows, err := tx.Query(ctx, listQuery(k2), args...)
	if err != nil {
		return fmt.Errorf("list rows: %w", err)
	}
	_, err = pgx.ForEachRow(rows, []any{&s.id, &code, &place, &network, &s.Ips, &s.Observations,
		&completed, &successes, &challenges, &blocks, &p50, &p90, &p99}, func() error {
		s.Completed, s.Successes, s.Challenges, s.Blocks = orNull8(completed), orNull8(successes), orNull8(challenges), orNull8(blocks)
		s.P50, s.P90, s.P99 = int32(orNull4(p50)), int32(orNull4(p90)), int32(orNull4(p99))
		s.code, s.place, s.network = code[0], int32(orNull4(place)), int32(orNull4(network))
		return each(&s)
	})
	if err != nil {
		return fmt.Errorf("list rows: %w", err)
	}
	return nil
}

func orNull8(v pgtype.Int8) int64 {
	if !v.Valid {
		return -1
	}
	return v.Int64
}

func orNull4(v pgtype.Int4) int64 {
	if !v.Valid {
		return -1
	}
	return int64(v.Int32)
}

// place is the shared Place of id, empty until readLabels fills it.
func (c *labels) place(id int32) *Place {
	if c.places[id] == nil {
		c.places[id] = &Place{}
		c.missing = append(c.missing, id)
	}
	return c.places[id]
}

func (c *labels) network(id int32) *Network {
	if c.networks[id] == nil {
		c.networks[id] = &Network{}
		c.missing = append(c.missing, -1-id)
	}
	return c.networks[id]
}

// readLabels fills the places and networks the last reads created.
func (c *labels) readLabels(ctx context.Context, tx pgx.Tx) error {
	var places, networks []int32
	for _, id := range c.missing {
		if id >= 0 {
			places = append(places, id)
		} else {
			networks = append(networks, -1-id)
		}
	}
	c.missing = nil
	var id int32
	var p Place
	var n Network
	for _, q := range []struct {
		sql  string
		ids  []int32
		dest []any
		fill func()
	}{
		{`SELECT place_id, country_code, country, region_code, region, city FROM public.place_names WHERE place_id = ANY($1)`,
			places, []any{&id, &p.CountryCode, &p.Country, &p.RegionCode, &p.Region, &p.City}, func() { *c.places[id] = p }},
		{`SELECT asn_label_id, asn, name, usage_type FROM public.asn_label_names WHERE asn_label_id = ANY($1)`,
			networks, []any{&id, &n.Asn, &n.Name, &n.UsageType}, func() { *c.networks[id] = n }},
	} {
		if len(q.ids) == 0 {
			continue
		}
		rows, _ := tx.Query(ctx, q.sql, q.ids)
		if _, err := pgx.ForEachRow(rows, q.dest, func() error { q.fill(); return nil }); err != nil {
			return fmt.Errorf("list labels: %w", err)
		}
	}
	return nil
}

// Row keys in jsonb order: a breakdown row's label keys per code plus the
// counters, and the pool lists' keys in proxy-index aggregate.pool_lists()
// order (its str() sort key).
var (
	labelKeys = map[byte]string{'c': "country_code country", 'r': "country_code country region_code region",
		'g': "country_code country region_code region city", 'a': "asn name usage_type",
		'x': "country_code country asn name usage_type", 'y': "country_code country region_code region city asn name usage_type"}
	poolKeys = map[string][]string{
		"geo":                 strings.Fields("country_code country region region_code city ips observations"),
		"countries_breakdown": strings.Fields("country_code country ips observations"),
		"asns_breakdown":      strings.Fields("asn name usage_type ips observations"),
	}
	rowColumns, poolColumns = map[byte][]column{}, map[string][]column{}
)

// column writes one key of a row.
type column struct {
	key   string
	write func([]byte, *ListRow) []byte
}

var noPlace, noNetwork = &Place{}, &Network{}

func (r *ListRow) where() *Place {
	if r.Place == nil {
		return noPlace
	}
	return r.Place
}

func (r *ListRow) net() *Network {
	if r.Network == nil {
		return noNetwork
	}
	return r.Network
}

func text(f func(*ListRow) *string) func([]byte, *ListRow) []byte {
	return func(b []byte, r *ListRow) []byte {
		if s := f(r); s != nil {
			return appendString(b, *s)
		}
		return append(b, "null"...)
	}
}

func number(f func(*ListRow) int64) func([]byte, *ListRow) []byte {
	return func(b []byte, r *ListRow) []byte {
		if v := f(r); v >= 0 {
			return strconv.AppendInt(b, v, 10)
		}
		return append(b, "null"...)
	}
}

// texts are a row's label strings by key.
var texts = map[string]func(*ListRow) *string{
	"country_code": func(r *ListRow) *string { return r.where().CountryCode },
	"country":      func(r *ListRow) *string { return r.where().Country },
	"region_code":  func(r *ListRow) *string { return r.where().RegionCode },
	"region":       func(r *ListRow) *string { return r.where().Region },
	"city":         func(r *ListRow) *string { return r.where().City },
	"name":         func(r *ListRow) *string { return r.net().Name },
	"usage_type":   func(r *ListRow) *string { return r.net().UsageType },
}

// writers write a row's value of each key as the payload has it.
var writers = map[string]func([]byte, *ListRow) []byte{
	"asn": func(b []byte, r *ListRow) []byte {
		if a := r.net().Asn; a != nil {
			return strconv.AppendInt(b, *a, 10)
		}
		return append(b, "null"...)
	},
	"ips":          number(func(r *ListRow) int64 { return r.Ips }),
	"observations": number(func(r *ListRow) int64 { return r.Observations }),
	"completed":    number(func(r *ListRow) int64 { return r.Completed }),
	"successes":    number(func(r *ListRow) int64 { return r.Successes }),
	"challenges":   number(func(r *ListRow) int64 { return r.Challenges }),
	"blocks":       number(func(r *ListRow) int64 { return r.Blocks }),
	"p50":          number(func(r *ListRow) int64 { return int64(r.P50) }),
	"p90":          number(func(r *ListRow) int64 { return int64(r.P90) }),
	"p99":          number(func(r *ListRow) int64 { return int64(r.P99) }),
	// Python's float repr as jsonb keeps it: shortest digits, ".0" when whole.
	"success_rate": func(b []byte, r *ListRow) []byte {
		if r.Completed <= 0 || r.Successes < 0 {
			return append(b, "null"...)
		}
		s := strconv.FormatFloat(float64(r.Successes)/float64(r.Completed), 'f', -1, 64)
		if !strings.Contains(s, ".") {
			s += ".0"
		}
		return append(b, s...)
	},
}

func init() {
	for k, f := range texts {
		writers[k] = text(f)
	}
	columns := func(keys []string) []column {
		out := []column{}
		for _, k := range keys {
			out = append(out, column{k, writers[k]})
		}
		sort.Slice(out, func(i, j int) bool { return jsonbBefore(out[i].key, out[j].key) })
		return out
	}
	for code, keys := range labelKeys {
		rowColumns[code] = columns(append(strings.Fields(keys), strings.Fields("ips observations completed successes success_rate challenges blocks p50 p90 p99")...))
	}
	for name, keys := range poolKeys {
		poolColumns[name] = columns(keys)
	}
}

// rowsJSON is rows as jsonb writes the payload's list.
func rowsJSON(columns []column, rows []*ListRow) []byte {
	b := []byte{'['}
	for i, r := range rows {
		if i > 0 {
			b = append(b, ", "...)
		}
		b = append(b, '{')
		for j, c := range columns {
			if j > 0 {
				b = append(b, ", "...)
			}
			b = c.write(append(b, `"`+c.key+`": `...), r)
		}
		b = append(b, '}')
	}
	return append(b, ']')
}

// pointers is rows as pointers.
func pointers(rows []ListRow) []*ListRow {
	out := make([]*ListRow, len(rows))
	for i := range out {
		out[i] = &rows[i]
	}
	return out
}

// appendString writes s as jsonb does (PostgreSQL escape_json).
func appendString(b []byte, s string) []byte {
	b = append(b, '"')
	for i := 0; i < len(s); i++ {
		c := s[i]
		if e := strings.IndexByte("\"\\\b\f\n\r\t", c); e >= 0 {
			b = append(b, '\\', `"\bfnrt`[e])
		} else if c < 0x20 {
			b = fmt.Appendf(b, `\u%04x`, c)
		} else {
			b = append(b, c)
		}
	}
	return append(b, '"')
}

// poolRows is the pool list name picked from its dimension's rows as
// proxy-index aggregate.pool_lists() picks it: rows with IPs, most IPs first,
// then by Python's str() of the row.
func poolRows(name string, l *List) []*ListRow {
	all := l.Rows(map[string]string{"geo": "city", "countries_breakdown": "country", "asns_breakdown": "asn"}[name])
	out := []*ListRow{}
	for i := range all {
		if all[i].Ips > 0 {
			out = append(out, &all[i])
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Ips != out[j].Ips {
			return out[i].Ips > out[j].Ips
		}
		return pyRepr(poolKeys[name], out[i]) < pyRepr(poolKeys[name], out[j])
	})
	return out
}

// pyRepr is Python's str() of a pool list row.
func pyRepr(keys []string, r *ListRow) string {
	var b strings.Builder
	b.WriteByte('{')
	for i, k := range keys {
		if i > 0 {
			b.WriteString(", ")
		}
		v := string(writers[k](nil, r))
		if f, ok := texts[k]; ok && f(r) != nil {
			v = pyStr(*f(r))
		} else if v == "null" {
			v = "None"
		}
		b.WriteString("'" + k + "': " + v)
	}
	return b.String() + "}"
}

// pyStr is Python's repr() of a str.
func pyStr(s string) string {
	q := '\''
	if strings.ContainsRune(s, '\'') && !strings.ContainsRune(s, '"') {
		q = '"'
	}
	var b strings.Builder
	b.WriteRune(q)
	for _, c := range s {
		switch e := strings.IndexRune("\t\n\r", c); {
		case c == q || c == '\\':
			b.WriteString(`\` + string(c))
		case e >= 0:
			b.WriteString(`\` + "tnr"[e:e+1])
		case unicode.IsPrint(c):
			b.WriteRune(c)
		case c < 0x100:
			fmt.Fprintf(&b, `\x%02x`, c)
		case c < 0x10000:
			fmt.Fprintf(&b, `\u%04x`, c)
		default:
			fmt.Fprintf(&b, `\U%08x`, c)
		}
	}
	b.WriteRune(q)
	return b.String()
}
