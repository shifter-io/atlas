package store

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math"
	"slices"

	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/jackc/pgx/v5"
)

// The published snapshot (proxy-index calculated/slim.py) keeps the top rows
// of each long list and records every list's full row count in
// breakdown_entries and pool.list_entries; the typed rows keep every row
// (lists.go). Payload v3 (proxy-index SNAP-LISTREF-1) leaves the arrays out:
// a window names its list and how many rows of each array it keeps,
// "lists": {"id": list_id, "entries": {name: rows}}, for the six breakdowns
// and the three pool lists (geo, countries_breakdown, asns_breakdown).
// breakdown_entries and pool.list_entries stay. Full puts the arrays back,
// so every reader sees a v2 payload:
//   - a v3 window's breakdowns are the first rows of each list by position,
//     and its pool lists the rows proxy-index aggregate.pool_lists() picks
//     from them (IPs above zero; most IPs first, then Python's str() order),
//     first rows kept;
//   - in both versions, the two lists the site reads whole are whole when
//     the typed rows hold the recorded count: pool.asns_breakdown in every
//     window, breakdowns.city_asn (read only for those groups) in every
//     window of each provider's group per proxy kind (snap.ByKind), the
//     group whose raw result the provider serves. Other v3 windows carry no
//     city_asn list.
// Rows are written as jsonb writes the published payload, so a v3 window
// reads back byte for byte as its v2 form.

// Full is the snapshot with its lists put back: the payload itself when
// nothing was capped.
func (s *SnapshotRow) Full() []byte {
	if s.full != nil {
		return s.full
	}
	return s.Payload
}

// listGroup is what readLists reads of a product group.
type listGroup struct {
	Provider  string                 `json:"provider"`
	Kind      string                 `json:"kind"`
	ProductID int64                  `json:"product_id"`
	Cohort    string                 `json:"cohort"`
	Windows   map[string]groupWindow `json:"windows"`
}

type groupWindow struct {
	Period struct {
		Hours int64 `json:"hours"`
	} `json:"period"`
	Metrics struct {
		Completed *float64 `json:"completed"`
	} `json:"metrics"`
	Lists *struct {
		ID int64 `json:"id"`
	} `json:"lists"`
}

// readLists sets Places and Full from the typed rows when the snapshot was
// slimmed, holding lists in cache (nil: a fresh one).
func (s *SnapshotRow) readLists(ctx context.Context, tx pgx.Tx, cache *Lists) error {
	if !bytes.Contains(s.Payload, []byte(`"breakdown_entries"`)) && !bytes.Contains(s.Payload, []byte(`"lists"`)) {
		return nil
	}
	if cache == nil {
		cache = NewLists()
	}
	pf, items, groups, err := s.groups()
	if err != nil {
		return err
	}
	ids, k2, err := s.windowLists(ctx, tx, groups)
	if err != nil {
		return err
	}
	picked := headlineGroups(groups)
	all, withCityAsn, seen := []int64{}, map[int64]bool{}, map[int64]bool{}
	for k, id := range ids {
		if !seen[id] {
			all, seen[id] = append(all, id), true
		}
		if picked[groupKey{k.Product, k.Cohort}] {
			withCityAsn[id] = true
		}
	}
	lists, err := cache.fetch(ctx, tx, k2, s.BuildID, all, withCityAsn)
	if err != nil {
		return err
	}
	s.Places = make(map[PlaceWindow]*List, len(ids))
	for k, id := range ids {
		s.Places[k] = lists[id]
	}
	return s.restore(pf, items, groups, picked)
}

// groups is the payload's products member, its elements and what readLists
// reads of each.
func (s *SnapshotRow) groups() (field, []field, []listGroup, error) {
	fields, err := objectFields(s.Payload)
	if err != nil {
		return field{}, nil, nil, err
	}
	pf, _ := member(fields, productsKey)
	items, err := arrayItems(s.Payload[pf.Start:pf.End])
	if err != nil {
		return field{}, nil, nil, fmt.Errorf("products: %w", err)
	}
	groups := make([]listGroup, len(items))
	for i, it := range items {
		if err := json.Unmarshal(s.Payload[pf.Start+it.Start:pf.Start+it.End], &groups[i]); err != nil {
			return field{}, nil, nil, fmt.Errorf("product group %d: %w", i, err)
		}
	}
	return pf, items, groups, nil
}

type groupKey struct {
	product int64
	cohort  string
}

// windowLists is each target-free window's list id: v3 windows name it; v2
// windows take calculated_windows.list_id (K2) or, before K2, result_id.
func (s *SnapshotRow) windowLists(ctx context.Context, tx pgx.Tx, groups []listGroup) (map[PlaceWindow]int64, bool, error) {
	var k2 bool
	if err := tx.QueryRow(ctx, `SELECT to_regclass('public.calculated_list_rows') IS NOT NULL`).Scan(&k2); err != nil {
		return nil, false, err
	}
	ids, v2 := map[PlaceWindow]int64{}, false
	for _, g := range groups {
		for _, w := range g.Windows {
			if w.Lists != nil {
				ids[PlaceWindow{g.ProductID, g.Cohort, w.Period.Hours}] = w.Lists.ID
			} else {
				v2 = true
			}
		}
	}
	if len(ids) > 0 && !k2 {
		return nil, false, fmt.Errorf("snapshot %d names lists, and calculated_list_rows does not exist", s.BuildID)
	}
	if !v2 {
		return ids, k2, nil
	}
	column := map[bool]string{true: "list_id", false: "result_id"}[k2]
	rows, _ := tx.Query(ctx, `SELECT product_id, cohort, window_hours, `+column+` FROM public.calculated_windows
 WHERE build_id = $1 AND target = '' AND `+column+` IS NOT NULL`, s.BuildID)
	var k PlaceWindow
	var id int64
	if _, err := pgx.ForEachRow(rows, []any{&k.Product, &k.Cohort, &k.Hours, &id}, func() error {
		if _, ok := ids[k]; !ok {
			ids[k] = id
		}
		return nil
	}); err != nil {
		return nil, false, fmt.Errorf("window lists: %w", err)
	}
	return ids, k2, nil
}

// headlineGroups is each provider's group per proxy kind (snap.ByKind).
func headlineGroups(groups []listGroup) map[groupKey]bool {
	byProvider := map[string][]*snap.Product{}
	for _, g := range groups {
		p := &snap.Product{Kind: g.Kind, ProductID: snap.Num(g.ProductID), Cohort: g.Cohort, Windows: map[string]snap.Window{}}
		if w, ok := g.Windows["30d"]; ok {
			p.Windows["30d"] = snap.Window{Metrics: snap.Metrics{Completed: numOrNaN(w.Metrics.Completed)}}
		}
		byProvider[g.Provider] = append(byProvider[g.Provider], p)
	}
	out := map[groupKey]bool{}
	for _, products := range byProvider {
		byKind, _ := snap.ByKind(products)
		for _, p := range byKind {
			out[groupKey{int64(p.ProductID), p.Cohort}] = true
		}
	}
	return out
}

func numOrNaN(v *float64) snap.Num {
	if v == nil {
		return snap.Num(math.NaN())
	}
	return snap.Num(*v)
}

// restore sets full: the payload with every window's lists put back.
func (s *SnapshotRow) restore(pf field, items []field, groups []listGroup, picked map[groupKey]bool) error {
	products := s.Payload[pf.Start:pf.End]
	repl := make([][]byte, len(items))
	for i, it := range items {
		g := groups[i]
		raw := products[it.Start:it.End]
		pfields, err := objectFields(raw)
		if err != nil {
			return err
		}
		wf, ok := member(pfields, "windows")
		if !ok {
			continue
		}
		wfields, err := objectFields(raw[wf.Start:wf.End])
		if err != nil {
			return err
		}
		windows := make([][]byte, len(wfields))
		for j, f := range wfields {
			w := g.Windows[f.Key]
			l := s.Places[PlaceWindow{g.ProductID, g.Cohort, w.Period.Hours}]
			if l == nil {
				continue
			}
			if windows[j], err = restoreWindow(raw[wf.Start+f.Start:wf.Start+f.End], l, picked[groupKey{g.ProductID, g.Cohort}]); err != nil {
				return fmt.Errorf("product %d %s window %s: %w", g.ProductID, g.Cohort, f.Key, err)
			}
		}
		if w := rebuild(raw[wf.Start:wf.End], wfields, windows); w != nil {
			repl[i] = splice(raw, wf, w)
		}
	}
	if p := rebuild(products, items, repl); p != nil {
		s.full = splice(s.Payload, pf, p)
	}
	return nil
}

// rebuild is raw with each span's value replaced by its non-nil repl, nil
// when none is.
func rebuild(raw []byte, spans []field, repl [][]byte) []byte {
	var out []byte
	at := 0
	for i, f := range spans {
		if repl[i] == nil {
			continue
		}
		out = append(append(out, raw[at:f.Start]...), repl[i]...)
		at = f.End
	}
	if out == nil {
		return nil
	}
	return append(out, raw[at:]...)
}

// breakdownNames are the breakdowns in jsonb key order, poolNames the pool lists.
var (
	breakdownNames = []string{"asn", "city", "region", "country", "city_asn", "country_asn"}
	poolNames      = []string{"geo", "asns_breakdown", "countries_breakdown"}
)

// restoreWindow is a window with its lists put back, nil when unchanged:
// a v2 window gets its whole city x ASN (when picked) and ASN lists, a v3
// window all its arrays, each as long as its lists.entries says, those two
// whole. A list is whole only when its rows match its recorded count
// (breakdown_entries, pool.list_entries); else a v2 list stays as published.
func restoreWindow(raw []byte, l *List, picked bool) ([]byte, error) {
	w, err := readWindowParts(raw)
	if err != nil {
		return nil, err
	}
	kept := map[string]int{"city_asn": -1, "asns_breakdown": -1} // -1: as published
	if w.v3 != nil {
		kept, w.bd = w.v3.Entries, []byte("{}")
	}
	changed := map[bool]bool{false: w.v3 != nil, true: w.v3 != nil} // per pool or breakdowns
	for _, name := range append(append([]string{}, breakdownNames...), poolNames...) {
		n, ok := kept[name]
		if !ok || name == "city_asn" && (!picked || l.Rows(name) == nil) {
			continue
		}
		set, err := w.restoreList(name, n, l)
		if err != nil {
			return nil, err
		}
		if set {
			changed[slices.Contains(poolNames, name)] = true
		}
	}
	if !changed[false] && !changed[true] {
		return nil, nil
	}
	return w.write(raw, changed)
}

// windowParts are a window's breakdowns and pool objects, their recorded list
// counts and, in a v3 window, its lists.
type windowParts struct {
	bd, pool             []byte
	entries, poolEntries map[string]int
	v3                   *struct {
		Entries map[string]int `json:"entries"`
	}
}

// readWindowParts reads raw's lists, pool {} when absent.
func readWindowParts(raw []byte) (*windowParts, error) {
	fields, err := objectFields(raw)
	if err != nil {
		return nil, err
	}
	w := &windowParts{bd: valueOf(raw, fields, "breakdowns"), pool: valueOf(raw, fields, "pool")}
	if w.pool == nil {
		w.pool = []byte("{}")
	}
	poolFields, err := objectFields(w.pool)
	if err != nil {
		return nil, fmt.Errorf("pool: %w", err)
	}
	if err := unmarshalAll(valueOf(raw, fields, "breakdown_entries"), &w.entries, valueOf(w.pool, poolFields, "list_entries"), &w.poolEntries,
		valueOf(raw, fields, "lists"), &w.v3); err != nil {
		return nil, err
	}
	return w, nil
}

// restoreList sets list name to its first n rows of l (all when whole), false
// when it stays as published.
func (w *windowParts) restoreList(name string, n int, l *List) (bool, error) {
	obj, recorded, columns, rows := &w.bd, w.entries, rowColumns[dimensionCodes[name]], pointers(l.Rows(name))
	if slices.Contains(poolNames, name) {
		obj, recorded, columns, rows = &w.pool, w.poolEntries, poolColumns[name], poolRows(name, l)
	}
	if (name == "city_asn" || name == "asns_breakdown") && recorded[name] == len(rows) && (w.v3 != nil || length(*obj, name) < len(rows)) {
		n = len(rows)
	} else if n < 0 {
		return false, nil
	}
	if n > len(rows) {
		return false, fmt.Errorf("the window keeps %d %s rows, its list has %d", n, name, len(rows))
	}
	var err error
	if *obj, err = setMember(*obj, name, rowsJSON(columns, rows[:n])); err != nil {
		return false, err
	}
	return true, nil
}

// write is raw with the changed breakdowns and pool, and without a v3
// window's lists.
func (w *windowParts) write(raw []byte, changed map[bool]bool) ([]byte, error) {
	var err error
	if w.v3 != nil {
		if raw, err = dropMember(raw, "lists"); err != nil {
			return nil, err
		}
	}
	for _, m := range []struct {
		key  string
		v    []byte
		pool bool
	}{{"breakdowns", w.bd, false}, {"pool", w.pool, true}} {
		if !changed[m.pool] {
			continue
		}
		if raw, err = setMember(raw, m.key, m.v); err != nil {
			return nil, err
		}
	}
	return raw, nil
}

// length is the element count of obj[key], 0 when absent or not an array.
func length(obj []byte, key string) int {
	fields, err := objectFields(obj)
	if err != nil {
		return 0
	}
	items, _ := arrayItems(valueOf(obj, fields, key))
	return len(items)
}

// setMember is the object obj (jsonb text) with key's value set to v:
// replaced where it is, else added where jsonb orders the key.
func setMember(obj []byte, key string, v []byte) ([]byte, error) {
	fields, err := objectFields(obj)
	if err != nil {
		return nil, err
	}
	if f, ok := member(fields, key); ok {
		return splice(obj, f, v), nil
	}
	at, before := 1, false
	for _, f := range fields {
		if jsonbBefore(f.Key, key) {
			at, before = f.End, true
		}
	}
	add := appendString(nil, key)
	add = append(append(add, ": "...), v...)
	switch {
	case before:
		add = append([]byte(", "), add...)
	case len(fields) > 0:
		add = append(add, ", "...)
	}
	return splice(obj, field{Start: at, End: at}, add), nil
}

// dropMember is the object obj (jsonb text) without key.
func dropMember(obj []byte, key string) ([]byte, error) {
	fields, err := objectFields(obj)
	if err != nil {
		return nil, err
	}
	keyStart := func(f field) int { return f.Start - len(": ") - len(appendString(nil, f.Key)) }
	for i, f := range fields {
		if f.Key != key {
			continue
		}
		cut := field{Start: keyStart(f), End: f.End}
		switch {
		case i > 0:
			cut.Start = fields[i-1].End
		case len(fields) > 1:
			cut.End = keyStart(fields[1])
		}
		return splice(obj, cut, nil), nil
	}
	return obj, nil
}

// arrayItems lists the elements of a JSON array, without decoding them.
func arrayItems(raw []byte) ([]field, error) {
	if len(raw) == 0 {
		return nil, nil
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	if t, err := dec.Token(); err != nil || t != json.Delim('[') {
		return nil, fmt.Errorf("not a JSON array")
	}
	out := []field{}
	for dec.More() {
		var v json.RawMessage
		if err := dec.Decode(&v); err != nil {
			return nil, err
		}
		end := int(dec.InputOffset())
		out = append(out, field{Start: end - len(v), End: end})
	}
	return out, nil
}

// unmarshalAll decodes each present (raw, target) pair; an absent raw is skipped.
func unmarshalAll(pairs ...any) error {
	for i := 0; i < len(pairs); i += 2 {
		if raw := pairs[i].(json.RawMessage); raw == nil {
			continue
		} else if err := json.Unmarshal(raw, pairs[i+1]); err != nil {
			return err
		}
	}
	return nil
}
