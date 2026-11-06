package store

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
)

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
