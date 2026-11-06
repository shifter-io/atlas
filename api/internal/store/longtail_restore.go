package store

import (
	"fmt"
	"slices"
)

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
