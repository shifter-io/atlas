package build

import (
	"sort"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
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
// its measured IPs. A window with no run of its group has no IP figure at all
// (NO-RUN-WINDOW-1): its unique IPs are null and it has no estimate, so the
// hour formula never fills a window without a test. This file is the only
// place the mapping lives.

// ranIn reports whether window w holds a run of its group: a completed
// request (the requests every answer shows for the window).
func ranIn(w snap.Window) bool { return w.Metrics.Completed.OK() && w.Metrics.Completed > 0 }

// estimateOf is window w's hour-formula figures, nil when it has none or no
// run.
func estimateOf(w snap.Window) *snap.Estimated {
	if !ranIn(w) {
		return nil
	}
	return w.Estimated
}

// ipsOf is a window's unique IPs (NaN with no run).
func ipsOf(w snap.Window) Num {
	if !ranIn(w) {
		return nan
	}
	if e := w.Estimated; e != nil && e.UniqueIps.Value.OK() {
		return Num(jsnum.Round(e.UniqueIps.Value.F()))
	}
	return w.Pool.UniqueIps
}

// scaledIps is the IPs of a place-list or breakdown row of window w whose
// measured IPs are ips.
func scaledIps(w snap.Window, ips Num) Num { return scaleBy(breakdownScale(w), ips) }

// breakdownScale is window w's breakdown scale, NaN when it has none.
func breakdownScale(w snap.Window) Num {
	if e := estimateOf(w); e != nil {
		return e.BreakdownScale
	}
	return nan
}

// scaleBy is a row's IPs ips under a window's breakdown scale (NaN: none).
func scaleBy(scale, ips Num) Num {
	if scale.OK() && ips.OK() {
		return Num(jsnum.Round(ips.F() * scale.F()))
	}
	return ips
}

// scaledShare is a row's share of window w's unique IPs, both as served.
func scaledShare(w snap.Window, ips Num) Num { return scaledIps(w, ips) / ipsOf(w) }

// ipsPhrase follows a window's unique IPs in the generated copy: the formula's
// figure counts more than the exit IPs the tests saw.
func ipsPhrase(w snap.Window) string {
	if e := estimateOf(w); e != nil && e.UniqueIps.Value.OK() {
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

// hoursOf is the window's released hours, oldest first (nil without figures
// or run; an hour whose start does not parse is left out).
func hoursOf(w snap.Window) []releasedHour {
	e := estimateOf(w)
	if e == nil {
		return nil
	}
	out := make([]releasedHour, 0, len(e.Series))
	for _, h := range e.Series {
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
// window has them, else one point per row, a daily row's IPs by dayIps. A
// window with no run has no point.
func ipPoints(w snap.Window, rangeID string, rows []snap.SeriesRow, age func(string) Num) []model.SeriesPoint {
	out := []model.SeriesPoint{}
	if !ranIn(w) {
		return out
	}
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
