package store

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math"

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
