package build

import (
	"sort"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// AllNetworks is the slug of the all-networks answer: /v1/networks/all and
// /v1/networks/all/places. A network whose slug is "all" would hide it, so
// the aggregate is left out in that case.
const AllNetworks = "all"

// allAnswer is every published product group of every network as one network,
// in a network answer's shape. Requests, successes and the place lists add up
// like a network's own groups (a place row's ipsSum stays an upper bound).
// The distinct figures (unique IPs over each range and per day, latency
// percentiles) are the snapshot's every-group fleet ("*"), where each exit IP
// counts once however many networks saw it; a snapshot without that entry
// leaves them null, never a sum. The summed ipsSum totals are null here.
func allAnswer(generatedAt string, s *snap.Snapshot, nr *netRead) (*NetworkAnswer, map[placeList][]PlaceRow, map[string][]placeSource) {
	groups := []*snap.Product{}
	providers, kinds := map[string]bool{}, map[string]bool{}
	if s != nil {
		for i := range s.Products {
			p := &s.Products[i]
			groups = append(groups, p)
			providers[p.Provider], kinds[proxyKind(p.Kind)] = true, true
		}
	}
	head := NetworkRow{Slug: AllNetworks, Source: model.SourceMeasured, Kind: AllNetworks, Kinds: []string{},
		Rank: nan, Score: nan, UniqueIps: nan, Countries: nan, LatencyP50: nan, SuccessRate: nan, ExclusiveIps: nan}
	for k := range kinds {
		head.Kinds = append(head.Kinds, k)
	}
	sort.Strings(head.Kinds)
	ans, lists, srcs := networkAnswer(generatedAt, head, AllNetworks, groups, nr)
	ans.Networks = len(providers)
	for _, r := range Ranges {
		t := ans.Ranges[r.ID]
		t.IpsSum, t.UniqueIps, t.LatencyP50, t.LatencyP90, t.LatencyP99 = nan, nan, nan, nan, nan
		if f := fleetAll(s, r.ID); f != nil {
			l := f.Metrics.LatencyMs
			t.UniqueIps, t.LatencyP50, t.LatencyP90, t.LatencyP99 = f.UniqueIps, l.P50, l.P90, l.P99
		}
	}
	distinct := map[string]Num{}
	if f := fleetAll(s, "30d"); f != nil {
		for _, sr := range f.Series {
			distinct[dayOf(sr.At)] = sr.UniqueIps
		}
	}
	for i := range ans.Days {
		d := &ans.Days[i]
		d.IpsSum, d.UniqueIps = nan, nan
		if v, ok := distinct[d.Day]; ok {
			d.UniqueIps = v
		}
	}
	month := ans.Ranges["30d"]
	ans.Network.UniqueIps, ans.Network.LatencyP50, ans.Network.SuccessRate = month.UniqueIps, month.LatencyP50, month.SuccessRate
	if len(groups) > 0 {
		ans.Network.Countries = Num(month.ExitCountries)
	}
	return ans, lists, srcs
}

// uniqueIpsCurve is /v1/home market.uniqueIpsCurve: the every-group fleet's
// distinct exit IPs per UTC hour (24h: 24 points) or UTC day (7d, 30d: 7, 30)
// over the whole window, each IP once however many networks saw it (a set
// union, so a point is at most the networks' sum and the range's uniqueIps
// at least any point), null where the slot has no IP (before the data starts
// too), the last slot the one holding the as-of time, partial; times are the
// slots' starts in minutes before the as-of time (SPARK-TIMES-1), the axis of
// successScoreCurve (curveAxis, #1301). Measured IPs, like market.uniqueIps: no
// hour formula (FLEET-POOL-SERIES-1, #1302). Empty without the fleet's slots.
func uniqueIpsCurve(s *snap.Snapshot, rangeID string) SparkSeries {
	out := SparkSeries{Values: []Num{}, Times: []Num{}}
	f := fleetAll(s, rangeID)
	if f == nil {
		return out
	}
	ref := snap.Ms(s.AsOf)
	for _, slot := range f.IPSlots {
		out.Values = append(out.Values, slot.UniqueIps)
		out.Times = append(out.Times, minutesAgo(ref, slot.At))
	}
	return out
}

func fleetAll(s *snap.Snapshot, window string) *snap.FleetRow {
	if s == nil {
		return nil
	}
	return s.FleetAll(window)
}

// dayOf is a series stamp's UTC day, as days() keys it.
func dayOf(at string) string {
	return at[:min(len(at), 10)]
}
