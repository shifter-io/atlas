package store

import (
	"bytes"
	"context"
	"encoding/json"

	"github.com/jackc/pgx/v5"
)

// The published snapshot (proxy-index calculated/slim.py) keeps the top rows of
// each long list and records every list's full row count in breakdown_entries
// and pool.list_entries; calculated_breakdowns keeps every row. The site reads
// two of those lists whole: the coverage evidence search over
// breakdowns.city_asn (passed through as Provider.measurement) and
// Provider.asns from pool.asns_breakdown. Both are read back from the build's
// typed rows, in the payload's row shape and order: city x ASN rows by
// position, ASNs with IPs by IPs, ties by the ASN's digits as the payload's
// Python sort compares them.
const longTailSQL = `SELECT w.product_id, w.cohort, w.window_hours,
 count(*) FILTER (WHERE d.dimension = 'city_asn'),
 coalesce(jsonb_agg(d.label || jsonb_build_object('ips', d.ips, 'observations', d.observations,
  'completed', d.completed, 'successes', d.successes, 'success_rate', d.success_rate,
  'challenges', d.challenges, 'blocks', d.blocks, 'p50', d.p50, 'p90', d.p90, 'p99', d.p99)
  ORDER BY d.position) FILTER (WHERE d.dimension = 'city_asn'), '[]')::text,
 count(*) FILTER (WHERE d.dimension = 'asn' AND d.ips > 0),
 coalesce(jsonb_agg(d.label || jsonb_build_object('ips', d.ips, 'observations', d.observations)
  ORDER BY d.ips DESC, (coalesce(d.label->>'asn', 'None') || ',') COLLATE "C", d.position) FILTER (WHERE d.dimension = 'asn' AND d.ips > 0), '[]')::text
 FROM public.calculated_windows w
 JOIN public.calculated_breakdown_details d ON d.result_id = w.result_id
 WHERE w.build_id = $1 AND w.target = '' AND d.dimension IN ('city_asn', 'asn')
 GROUP BY 1, 2, 3`

type tailKey struct {
	product int64
	cohort  string
	hours   int64
}

// longTail is one product window's full city x ASN and ASN lists.
type longTail struct {
	cityAsnCount, asnsCount int
	cityAsn, asns           json.RawMessage
}

// Full is the snapshot with every capped list the site reads whole put back:
// the payload itself when nothing was capped.
func (s *SnapshotRow) Full() []byte {
	if s.full != nil {
		return s.full
	}
	return s.Payload
}

// readLongTail sets Full from the build's typed rows when the snapshot is capped.
func (s *SnapshotRow) readLongTail(ctx context.Context, tx pgx.Tx) error {
	if !bytes.Contains(s.Payload, []byte(`"breakdown_entries"`)) {
		return nil
	}
	tails := map[tailKey]longTail{}
	rows, err := tx.Query(ctx, longTailSQL, s.BuildID)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var k tailKey
		var t longTail
		var cityAsn, asns string
		if err := rows.Scan(&k.product, &k.cohort, &k.hours, &t.cityAsnCount, &cityAsn, &t.asnsCount, &asns); err != nil {
			return err
		}
		t.cityAsn, t.asns = json.RawMessage(cityAsn), json.RawMessage(asns)
		tails[k] = t
	}
	if err := rows.Err(); err != nil {
		return err
	}
	s.full, err = restoreLongTail(s.Payload, tails)
	return err
}

// restoreLongTail puts the full lists into every window that recorded more
// rows than it kept. A list whose typed rows do not match its recorded count
// is left as published. Returns nil when nothing changed.
func restoreLongTail(payload []byte, tails map[tailKey]longTail) ([]byte, error) {
	var doc map[string]json.RawMessage
	var products []map[string]json.RawMessage
	if err := json.Unmarshal(payload, &doc); err != nil {
		return nil, err
	}
	if err := json.Unmarshal(doc["products"], &products); err != nil {
		return nil, err
	}
	changed := false
	for _, p := range products {
		touched, err := restoreProduct(p, tails)
		if err != nil {
			return nil, err
		}
		changed = changed || touched
	}
	if !changed {
		return nil, nil
	}
	var err error
	if doc["products"], err = json.Marshal(products); err != nil {
		return nil, err
	}
	return json.Marshal(doc)
}

func restoreProduct(p map[string]json.RawMessage, tails map[tailKey]longTail) (bool, error) {
	var k tailKey
	var windows map[string]map[string]json.RawMessage
	if err := unmarshalAll(p["product_id"], &k.product, p["cohort"], &k.cohort, p["windows"], &windows); err != nil {
		return false, err
	}
	touched := false
	for _, w := range windows {
		var period struct {
			Hours int64 `json:"hours"`
		}
		var pool map[string]json.RawMessage
		if err := unmarshalAll(w["period"], &period, w["pool"], &pool); err != nil {
			return false, err
		}
		k.hours = period.Hours
		t, ok := tails[k]
		if !ok {
			continue
		}
		cities, err := replace(w, "breakdowns", "city_asn", w["breakdown_entries"], t.cityAsnCount, t.cityAsn)
		if err != nil {
			return false, err
		}
		asns, err := replace(w, "pool", "asns_breakdown", pool["list_entries"], t.asnsCount, t.asns)
		if err != nil {
			return false, err
		}
		touched = touched || cities || asns
	}
	if !touched {
		return false, nil
	}
	var err error
	p["windows"], err = json.Marshal(windows)
	return err == nil, err
}

// replace sets window[section][name] to rows when the recorded entry count of
// the list is count and the published list is shorter.
func replace(window map[string]json.RawMessage, section, name string, entries json.RawMessage, count int, rows json.RawMessage) (bool, error) {
	if window[section] == nil || entries == nil {
		return false, nil
	}
	var recorded map[string]int
	var lists map[string]json.RawMessage
	if err := unmarshalAll(entries, &recorded, window[section], &lists); err != nil {
		return false, err
	}
	var kept []json.RawMessage
	if err := unmarshalAll(lists[name], &kept); err != nil {
		return false, err
	}
	n, ok := recorded[name]
	if !ok || n != count || len(kept) >= n {
		return false, nil
	}
	lists[name] = rows
	var err error
	window[section], err = json.Marshal(lists)
	return err == nil, err
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
