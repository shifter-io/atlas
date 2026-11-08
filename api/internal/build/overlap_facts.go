package build

import (
	"math"
	"sort"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// The overlap rules every answer shares (/v1/overlap, and W6's home), ported
// from the old site (proxy-index-api src/lib/ownership.ts ownershipFor, and
// the resale rule of its methodology page and demo generator). They are
// quoted in api/answers/overlap.md.

// Ownership labels.
const (
	LabelFullyExclusive  = "fully_exclusive"
	LabelMostlyExclusive = "mostly_exclusive"
	LabelMixed           = "mixed"
	LabelResale          = "resale"
)

const (
	// mostlyExclusiveFrom and mixedFrom are ownershipFor's exclusive-share
	// thresholds: "Mostly exclusive" from 0.75, "Mixed sourcing" from 0.4,
	// "Resale signal" below.
	mostlyExclusiveFrom = 0.75
	mixedFrom           = 0.4
	// A network's largest pair is its likely upstream when more than
	// upstreamShareAbove of its pool is in the other network and that is
	// more than upstreamRatioAbove times the other's share inside it.
	upstreamShareAbove = 0.45
	upstreamRatioAbove = 3
)

// Market bases: where market.distinctIps comes from.
const (
	// BasisKindFleet: the kind's fleet entry (cohort "*:<kind>"), each IP once.
	BasisKindFleet = "kind_fleet"
	// BasisPoolsMinusPairs: sumOfPools − sharedIps, which counts an IP held
	// by three or more pools more than once.
	BasisPoolsMinusPairs = "sum_of_pools_minus_shared_ips"
)

// OverlapPeer is one network another one shares exit IPs with, in one range.
type OverlapPeer struct {
	Slug          string `json:"slug"`
	SharedIps     Num    `json:"sharedIps"`
	ShareOfOurs   Num    `json:"shareOfOurs"`
	ShareOfTheirs Num    `json:"shareOfTheirs"`
	name          string
}

// OverlapUpstream is the network a pool sits mostly inside.
type OverlapUpstream struct {
	Slug        string `json:"slug"`
	Name        string `json:"name"`
	ShareOfOurs Num    `json:"shareOfOurs"`
}

// OverlapMarket is a kind's duplication in one range, on one basis: the
// range's measured pools and pairs of that kind.
type OverlapMarket struct {
	Networks        int      `json:"networks"`
	SumOfPools      Num      `json:"sumOfPools"`
	SharedIps       Num      `json:"sharedIps"`
	DistinctIps     Num      `json:"distinctIps"`
	DuplicatedShare Num      `json:"duplicatedShare"`
	Basis           string   `json:"basis"`
	LikelyResellers []string `json:"likelyResellers"`
}

// OwnershipLabel is ownershipFor's label of a pool's exclusive and shared
// share, nil when either is unknown (the old site's "N/A" and "Not compared
// yet"). Fully exclusive needs exactly 1 and 0: a tiny overlap is not
// rounded away.
func OwnershipLabel(exclusive, shared Num) model.Str {
	switch {
	case !exclusive.OK() || !shared.OK():
		return nil
	case exclusive == 1 && shared == 0:
		return model.S(LabelFullyExclusive)
	case exclusive >= mostlyExclusiveFrom:
		return model.S(LabelMostlyExclusive)
	case exclusive >= mixedFrom:
		return model.S(LabelMixed)
	}
	return model.S(LabelResale)
}

// LikelyUpstream is the network whose pool holds most of ours: the largest
// pair (peers are most shared first, so most of our pool first) when its
// shareOfOurs is above 45% and above 3 times its shareOfTheirs. Nil otherwise.
func LikelyUpstream(peers []OverlapPeer) *OverlapUpstream {
	if len(peers) == 0 {
		return nil
	}
	top := peers[0]
	if !top.ShareOfOurs.OK() || !top.ShareOfTheirs.OK() ||
		top.ShareOfOurs <= upstreamShareAbove || top.ShareOfOurs <= upstreamRatioAbove*top.ShareOfTheirs {
		return nil
	}
	return &OverlapUpstream{Slug: top.Slug, Name: top.name, ShareOfOurs: top.ShareOfOurs}
}

// MarketOverlap is the market of nets, a kind's networks in one range.
// fleetDistinct is the kind's fleet entry's unique IPs for the range (NaN
// when the snapshot publishes none): it is distinctIps when known.
func MarketOverlap(nets []OverlapNetwork, fleetDistinct Num) OverlapMarket {
	m := OverlapMarket{Networks: len(nets), LikelyResellers: []string{}}
	var sum, shared float64
	pairs := map[[2]string]bool{}
	resellers := []*OverlapNetwork{}
	for i := range nets {
		n := &nets[i]
		if n.PoolIps.OK() {
			sum += n.PoolIps.F()
		}
		for _, p := range n.Peers {
			k := [2]string{min(n.Slug, p.Slug), max(n.Slug, p.Slug)}
			if !pairs[k] && p.SharedIps.OK() {
				pairs[k] = true
				shared += p.SharedIps.F()
			}
		}
		if n.LikelyUpstream != nil {
			resellers = append(resellers, n)
		}
	}
	sort.SliceStable(resellers, func(i, j int) bool {
		return resellers[i].LikelyUpstream.ShareOfOurs > resellers[j].LikelyUpstream.ShareOfOurs
	})
	for _, n := range resellers {
		m.LikelyResellers = append(m.LikelyResellers, n.Slug)
	}
	m.SumOfPools, m.SharedIps = Num(sum), Num(shared)
	m.DistinctIps, m.Basis = fleetDistinct, BasisKindFleet
	if !fleetDistinct.OK() {
		m.DistinctIps, m.Basis = Num(math.Max(0, sum-shared)), BasisPoolsMinusPairs
	}
	m.DuplicatedShare = nan
	if sum > 0 {
		m.DuplicatedShare = Num(math.Max(0, 1-m.DistinctIps.F()/sum))
	}
	return m
}

// kindFleetDistinct is the unique IPs of the kind's fleet entry (cohort
// "*:<kind>", CALC-PAGE-INPUTS-1) in the window, NaN when it is not published.
func kindFleetDistinct(s *snap.Snapshot, kind, window string) Num {
	if s == nil {
		return nan
	}
	for _, f := range s.Fleet {
		if f.Cohort == snap.AllCohorts+":"+kind && f.Window == window {
			return f.UniqueIps
		}
	}
	return nan
}
