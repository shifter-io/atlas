package testscale

import (
	"encoding/json"
	"sort"

	"github.com/shifter-io/atlas/api/internal/snap"
	"github.com/shifter-io/atlas/api/internal/store"
)

// Listed is a snapshot payload before v4 (whole, with its product groups)
// with each window's lists made from one source, the rows its v4 build
// (NetworkHead) has in the window's network list: every breakdown a network
// list has, their row counts, and the pool's country and ASN lists, which
// are those rows with IPs, most first (ties in list order, as the
// framework's pool lists already are). A v4 build reads all of them from its
// list, so its answers are the build of Listed(payload)'s, with
// AsV4Answers' differences; the saved fixtures' pool lists and breakdowns
// disagree, so the build of the payload itself answers differently.
func Listed(payload []byte) ([]byte, error) {
	var doc map[string]any
	var typed snap.Snapshot
	err := json.Unmarshal(payload, &doc)
	if err == nil {
		err = json.Unmarshal(payload, &typed)
	}
	if err != nil {
		return nil, err
	}
	row := &store.SnapshotRow{Payload: payload}
	for i, g := range asList(doc["products"]) {
		p := &typed.Products[i]
		for label, w := range g.(map[string]any)["windows"].(map[string]any) {
			listedWindow(w.(map[string]any), windowRows(row, p, p.Windows[label]))
		}
	}
	return json.Marshal(doc)
}

// listedWindow sets window w's breakdowns, breakdown_entries and pool
// country and ASN lists from its network list rows.
func listedWindow(w map[string]any, rows map[string][]store.NetRow) {
	pool, _ := w["pool"].(map[string]any)
	if pool == nil {
		pool = map[string]any{}
	}
	pool["countries_breakdown"] = poolList(rows["country"], "country_code", "country")
	pool["asns_breakdown"] = poolList(rows["asn"], "asn", "name", "usage_type")
	w["breakdowns"], w["breakdown_entries"], w["pool"] = BreakdownRows(rows), RowCounts(rows), pool
}

// BreakdownRows is a network list's rows as breakdown rows, by breakdown
// name.
func BreakdownRows(rows map[string][]store.NetRow) map[string][]map[string]any {
	out := map[string][]map[string]any{}
	for _, name := range netNames {
		out[name] = make([]map[string]any, 0, len(rows[name]))
		for _, r := range rows[name] {
			out[name] = append(out[name], breakdownRow(r))
		}
	}
	return out
}

// breakdownRow is a network list row as a breakdown row: what a list does
// not keep (observations, challenges, blocks, p90 and p99) is null.
func breakdownRow(r store.NetRow) map[string]any {
	out := labels(r)
	for k, v := range map[string]int64{"ips": r.Ips, "completed": r.Completed, "successes": r.Successes, "p50": int64(r.P50)} {
		out[k] = nullable(v)
	}
	out["success_rate"] = nil
	if r.Completed > 0 && r.Successes >= 0 {
		out["success_rate"] = float64(r.Successes) / float64(r.Completed)
	}
	for _, k := range []string{"observations", "challenges", "blocks", "p90", "p99"} {
		out[k] = nil
	}
	return out
}

// labels is a list row's place and network labels by payload key.
func labels(r store.NetRow) map[string]any {
	out := map[string]any{}
	if p := r.Place; p != nil {
		out["country_code"], out["country"], out["region_code"], out["region"], out["city"] = p.CountryCode, p.Country, p.RegionCode, p.Region, p.City
	}
	if n := r.Network; n != nil {
		out["asn"], out["name"], out["usage_type"] = n.Asn, n.Name, n.UsageType
	}
	return out
}

// poolList is the rows with IPs, most first, as a pool list with keys.
func poolList(rows []store.NetRow, keys ...string) []any {
	var with []store.NetRow
	for _, r := range rows {
		if r.Ips > 0 {
			with = append(with, r)
		}
	}
	sort.SliceStable(with, func(i, j int) bool { return with[i].Ips > with[j].Ips })
	out := []any{}
	for _, r := range with {
		l, row := labels(r), map[string]any{"ips": r.Ips, "observations": nil}
		for _, k := range keys {
			row[k] = l[k]
		}
		out = append(out, row)
	}
	return out
}

// nullable is a list row counter, -1 for null.
func nullable(v int64) any {
	if v < 0 {
		return nil
	}
	return v
}
