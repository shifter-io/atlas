package build

import (
	"encoding/json"
	"sort"
	"time"

	"github.com/Zeus-Development/proxy-index-website/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
	"github.com/Zeus-Development/proxy-index-website/api/internal/snap"
)

// A daily group's window whose tests have released hours carries the hour
// formula's figures beside its measured ones (snap.Estimated, proxy-index
// estimates/window.py). Every IP count the API derives from such a window
// comes from them, as a plain whole number with no extra flag, label or
// field (contract K8):
//
//   - the window's unique IPs (totals, tiles, ranks, provider pool 24h/30d)
//     are the formula's window figure;
//   - a place-list or breakdown row's IPs (country, region, city, ASN,
//     city x ASN, the pool's geo, country and ASN lists) are its measured IPs
//     times the window's breakdown scale, rounded half up;
//   - the 24h IP chart has one point per released hour, and a daily (7d, 30d)
//     chart point is the measured day's IPs times the breakdown scale,
//     rounded half up, like a place row (owner decision on review B1).
//
// Requests, success, latency, uptime, outages and overlap stay measured, and a
// window without the figures (census-pulse groups, no released hour) keeps
// its measured IPs. This file is the only place the mapping lives.

// ipsOf is a window's unique IPs.
func ipsOf(w snap.Window) Num {
	if e := w.Estimated; e != nil && e.UniqueIps.Value.OK() {
		return Num(jsnum.Round(e.UniqueIps.Value.F()))
	}
	return w.Pool.UniqueIps
}

// scaledIps is the IPs of a place-list or breakdown row of window w whose
// measured IPs are ips.
func scaledIps(w snap.Window, ips Num) Num {
	if e := w.Estimated; e != nil && e.BreakdownScale.OK() && ips.OK() {
		return Num(jsnum.Round(ips.F() * e.BreakdownScale.F()))
	}
	return ips
}

// scaledShare is a row's share of window w's unique IPs, both as served.
func scaledShare(w snap.Window, ips Num) Num { return scaledIps(w, ips) / ipsOf(w) }

// ipsPhrase follows a window's unique IPs in the generated copy: the formula's
// figure counts more than the exit IPs the tests saw.
func ipsPhrase(w snap.Window) string {
	if e := w.Estimated; e != nil && e.UniqueIps.Value.OK() {
		return " unique exit IPs"
	}
	return " distinct exit IPs observed"
}

// releasedHour is one released hour of a window's figures.
type releasedHour struct {
	stamp string
	at    time.Time
	ips   float64
}

// hoursOf is the window's released hours, oldest first (nil without figures;
// an hour whose start does not parse is left out).
func hoursOf(w snap.Window) []releasedHour {
	if w.Estimated == nil {
		return nil
	}
	out := make([]releasedHour, 0, len(w.Estimated.Series))
	for _, h := range w.Estimated.Series {
		if at, ok := snap.ParseTime(h.Start); ok && h.UniqueIps.OK() {
			out = append(out, releasedHour{stamp: h.Start, at: at, ips: h.UniqueIps.F()})
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].at.Before(out[j].at) })
	return out
}

// dayIps is, for window w, the IPs of a daily series row: its measured IPs
// scaled like a place row's.
func dayIps(w snap.Window, r snap.SeriesRow) Num { return scaledIps(w, r.UniqueIps) }

// ipPoints is window w's IP chart over its series rows (age gives a point's
// T from a stamp): in the 24h range one point per released hour when the
// window has them, else one point per row, a daily row's IPs by dayIps.
func ipPoints(w snap.Window, rangeID string, rows []snap.SeriesRow, age func(string) Num) []model.SeriesPoint {
	out := []model.SeriesPoint{}
	if rangeID == "24h" {
		if hours := hoursOf(w); len(hours) > 0 {
			for _, h := range hours {
				out = append(out, model.SeriesPoint{T: age(h.stamp), V: Num(jsnum.Round(h.ips))})
			}
			return out
		}
		for _, r := range rows {
			out = append(out, model.SeriesPoint{T: age(r.At), V: r.UniqueIps})
		}
		return out
	}
	for _, r := range rows {
		out = append(out, model.SeriesPoint{T: age(r.At), V: dayIps(w, r)})
	}
	return out
}

// measurementOf is the product's raw result as the API serves it
// (Provider.measurement): unchanged unless a window has the formula's
// figures; such a window loses its `estimated` block and its IP counts follow
// the mapping above (24h series buckets, finer than the released hours, have
// a null unique_ips). Members of rewritten objects come out in key order.
func measurementOf(p *snap.Product) (json.RawMessage, error) {
	estimated := false
	for _, w := range p.Windows {
		estimated = estimated || w.Estimated != nil
	}
	if !estimated {
		return p.Raw, nil
	}
	var product, windows map[string]json.RawMessage
	if err := json.Unmarshal(p.Raw, &product); err != nil {
		return nil, err
	}
	if err := json.Unmarshal(product["windows"], &windows); err != nil {
		return nil, err
	}
	for label, raw := range windows {
		w := p.Windows[label]
		if w.Estimated == nil {
			continue
		}
		out, err := rawWindow(raw, w, label)
		if err != nil {
			return nil, err
		}
		windows[label] = out
	}
	return setRaw(product, "windows", windows)
}

type rawObject = map[string]json.RawMessage

// rawWindow is a raw window with its IP counts mapped and no `estimated`.
func rawWindow(raw json.RawMessage, w snap.Window, label string) (json.RawMessage, error) {
	var win, pool rawObject
	if err := json.Unmarshal(raw, &win); err != nil {
		return nil, err
	}
	delete(win, "estimated")
	if err := unmarshalSet(win["pool"], &pool); err != nil {
		return nil, err
	}
	if pool != nil {
		pool["unique_ips"] = numJSON(ipsOf(w))
		for _, name := range []string{"geo", "countries_breakdown", "asns_breakdown"} {
			if err := scaleRows(pool, name, w); err != nil {
				return nil, err
			}
		}
		if err := scaleDaily(pool, w); err != nil {
			return nil, err
		}
		if err := setMember(win, "pool", pool); err != nil {
			return nil, err
		}
	}
	var breakdowns rawObject
	if err := unmarshalSet(win["breakdowns"], &breakdowns); err != nil {
		return nil, err
	}
	for name := range breakdowns {
		if err := scaleRows(breakdowns, name, w); err != nil {
			return nil, err
		}
	}
	if breakdowns != nil {
		if err := setMember(win, "breakdowns", breakdowns); err != nil {
			return nil, err
		}
	}
	if err := mapSeries(win, w, label); err != nil {
		return nil, err
	}
	return json.Marshal(win)
}

// scaleRows maps the ips of every row of obj[name] (left alone when absent).
func scaleRows(obj rawObject, name string, w snap.Window) error {
	var rows []rawObject
	if err := unmarshalSet(obj[name], &rows); err != nil || rows == nil {
		return err
	}
	for _, r := range rows {
		if err := scaleMember(r, "ips", w); err != nil {
			return err
		}
	}
	return setMember(obj, name, rows)
}

// scaleDaily maps the pool's daily average unique_ips (left alone when absent).
func scaleDaily(pool rawObject, w snap.Window) error {
	var daily rawObject
	if err := unmarshalSet(pool["daily"], &daily); err != nil || daily == nil {
		return err
	}
	if err := scaleMember(daily, "unique_ips", w); err != nil {
		return err
	}
	return setMember(pool, "daily", daily)
}

// scaleMember scales the number obj[key] like a place row's IPs (left alone
// when absent).
func scaleMember(obj rawObject, key string, w snap.Window) error {
	raw, ok := obj[key]
	if !ok {
		return nil
	}
	var ips Num
	if err := json.Unmarshal(raw, &ips); err != nil {
		return err
	}
	obj[key] = numJSON(scaledIps(w, ips))
	return nil
}

// mapSeries maps the unique_ips of the window's series rows.
func mapSeries(win rawObject, w snap.Window, label string) error {
	var rows []rawObject
	if err := unmarshalSet(win["series"], &rows); err != nil || rows == nil {
		return err
	}
	for _, r := range rows {
		if _, ok := r["unique_ips"]; ok && label == "24h" {
			r["unique_ips"] = numJSON(jsnum.NaN)
		} else if err := scaleMember(r, "unique_ips", w); err != nil {
			return err
		}
	}
	return setMember(win, "series", rows)
}

// unmarshalSet decodes raw into v when raw is present and not null.
func unmarshalSet(raw json.RawMessage, v any) error {
	if len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	return json.Unmarshal(raw, v)
}

func setMember(obj rawObject, key string, v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	obj[key] = b
	return nil
}

func setRaw(obj rawObject, key string, v any) (json.RawMessage, error) {
	if err := setMember(obj, key, v); err != nil {
		return nil, err
	}
	return json.Marshal(obj)
}

func numJSON(v Num) json.RawMessage {
	b, _ := v.MarshalJSON() // a Num always encodes
	return b
}
