package build

import (
	"math"

	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// poolSlot is one slot a pool comparison counts: its unique IPs, its
// requests (NaN for a released hour, which has none) and its end in ms.
type poolSlot struct {
	ips, requests, end float64
}

// poolChange is a range's pool move for the changelog, only from complete
// sides (api/answers/changelog.md, "Complete pool comparisons"): the opening
// and closing fifth of the range's counted slots (countedDays, countedHours)
// in percent, their two means, and the end of the newest slot compared (ms).
// The move is NaN when fewer than two slots count, the two sides were not
// sampled alike (sameSampling) or the opening side has no IPs.
func poolChange(w snap.Window, rangeID string) (pct float64, edges trendEdges, at float64) {
	none := trendEdges{nan, nan}
	slots := countedDays(w)
	if rangeID == "24h" {
		slots = countedHours(w)
	}
	if len(slots) < 2 {
		return math.NaN(), none, math.NaN()
	}
	e := edge(len(slots))
	opening, closing := slots[:e], slots[len(slots)-e:]
	was, now := slotMean(opening, false), slotMean(closing, false)
	if !sameSampling(opening, closing) || !(was > 0) {
		return math.NaN(), none, math.NaN()
	}
	return (now - was) / was * 100, trendEdges{Num(was), Num(now)}, closing[len(closing)-1].end
}

// countedDays is the complete days of a 7d or 30d window's daily series
// rows (24 hours from the window's start): a day with requests and unique
// IPs (dayIps) whose day before is in the window and has requests too (so
// not the day measurement started in: a new network, a gap, a database
// copy), and that ends before the window's end (the day ending at the
// cutoff misses the runs still running then: a run is stored when it ends).
func countedDays(w snap.Window) []poolSlot {
	out := []poolSlot{}
	cutoff := snap.Ms(w.Period.End)
	if !ranIn(w) {
		return out
	}
	for i := 1; i < len(w.Series); i++ {
		r, ips := w.Series[i], dayIps(w, w.Series[i])
		end := snap.Ms(r.At) + dayMs
		if !sentRequests(w.Series[i-1]) || !sentRequests(r) || !(ips > 0) || !(end < cutoff) {
			continue
		}
		out = append(out, poolSlot{ips: ips.F(), requests: r.Completed.F(), end: end})
	}
	return out
}

// countedHours is the 24h window's released hours (each a whole hour), or,
// in a window without them, its series rows (hours, or five minutes before
// SERIES-HOURLY-DAILY-1: rowSpan) with requests and unique IPs, each ending
// at most at the window's end.
func countedHours(w snap.Window) []poolSlot {
	out := []poolSlot{}
	if hours := hoursOf(w); len(hours) > 0 {
		for _, h := range hours {
			out = append(out, poolSlot{ips: jsnum.Round(h.ips), requests: math.NaN(), end: snap.Ms(h.stamp) + 3600e3})
		}
		return out
	}
	if !ranIn(w) {
		return out
	}
	span, cutoff := float64(rowSpan(w, "24h").Milliseconds()), snap.Ms(w.Period.End)
	for _, r := range w.Series {
		if sentRequests(r) && r.UniqueIps.OK() && r.UniqueIps > 0 {
			end := math.Min(snap.Ms(r.At)+span, cutoff)
			out = append(out, poolSlot{ips: r.UniqueIps.F(), requests: r.Completed.F(), end: end})
		}
	}
	return out
}

// sentRequests reports whether a series row has requests.
func sentRequests(r snap.SeriesRow) bool { return r.Completed.OK() && r.Completed > 0 }

// sameSampling reports whether a pool comparison's two sides were measured
// alike: each sent at least PoolRequestRatio of the other's requests per
// slot, since the unique IPs seen follow the requests sent. Slots without
// request counts (released hours) always are.
func sameSampling(a, b []poolSlot) bool {
	ra, rb := slotMean(a, true), slotMean(b, true)
	if math.IsNaN(ra) || math.IsNaN(rb) {
		return true
	}
	return math.Min(ra, rb) >= PoolRequestRatio*math.Max(ra, rb)
}

// slotMean is the mean unique IPs of slots, or with requests their mean
// requests.
func slotMean(slots []poolSlot, requests bool) float64 {
	xs := make([]float64, len(slots))
	for i, s := range slots {
		xs[i] = s.ips
		if requests {
			xs[i] = s.requests
		}
	}
	return mean(xs)
}
