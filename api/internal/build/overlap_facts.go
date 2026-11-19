package build

import (
	"math"
	"sort"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
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

// BasisKindFleet is where market.distinctIps comes from: the kind's fleet
// entry (cohort "*:<kind>"), each IP once however many networks saw it.
const BasisKindFleet = "kind_fleet"

// OverlapPeer is one network another one shares exit IPs with, in one range.
type OverlapPeer struct {
	Slug          string `json:"slug"`
	SharedIps     Num    `json:"sharedIps"`
	ShareOfOurs   Num    `json:"shareOfOurs"`
	ShareOfTheirs Num    `json:"shareOfTheirs"`
	name          string
	// observed is the pair's measured shared IPs (the market's).
	observed Num
}

// OverlapUpstream is the network a pool sits mostly inside.
type OverlapUpstream struct {
	Slug        string `json:"slug"`
	Name        string `json:"name"`
	ShareOfOurs Num    `json:"shareOfOurs"`
}

// OverlapMarket is a kind's duplication in one range, on one basis: the
// range's measured pools and pairs of that kind. Its IP counts are the
// measured ones × sumOfPools ÷ observedSumOfPools (EXCLUSIVE-ESTIMATE-1),
// so duplicatedShare is the measured one.
type OverlapMarket struct {
	Networks           int       `json:"networks"`
	SumOfPools         Num       `json:"sumOfPools"`
	ObservedSumOfPools Num       `json:"observedSumOfPools"`
	SharedIps          Num       `json:"sharedIps"`
	DistinctIps        Num       `json:"distinctIps"`
	DuplicatedShare    Num       `json:"duplicatedShare"`
	Basis              model.Str `json:"basis"`
	LikelyResellers    []string  `json:"likelyResellers"`
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
// fleetDistinct is the kind's fleet entry's unique IPs for the range: it is
// distinctIps. NaN (the snapshot publishes none) leaves distinctIps,
// duplicatedShare and basis null: no sum of pools minus pairs stands in, as it
// counts an IP of k networks k(k−1)/2 times.
func MarketOverlap(nets []OverlapNetwork, fleetDistinct Num) OverlapMarket {
	m := OverlapMarket{Networks: len(nets), LikelyResellers: []string{}}
	var sum, observed, shared float64
	pairs := map[[2]string]bool{}
	resellers := []*OverlapNetwork{}
	for i := range nets {
		n := &nets[i]
		if n.PoolIps.OK() {
			sum += n.PoolIps.F()
		}
		if n.ObservedPoolIps.OK() {
			observed += n.ObservedPoolIps.F()
		}
		for _, p := range n.Peers {
			k := [2]string{min(n.Slug, p.Slug), max(n.Slug, p.Slug)}
			if !pairs[k] && p.observed.OK() {
				pairs[k] = true
				shared += p.observed.F()
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
	scale := Num(1)
	if observed > 0 {
		scale = Num(sum / observed)
	}
	m.SumOfPools, m.ObservedSumOfPools, m.SharedIps = Num(sum), Num(observed), ofPool(Num(shared), scale)
	m.DistinctIps, m.DuplicatedShare = ofPool(fleetDistinct, scale), nan
	if fleetDistinct.OK() {
		m.Basis = model.S(BasisKindFleet)
	}
	if fleetDistinct.OK() && observed > 0 {
		m.DuplicatedShare = Num(math.Max(0, 1-fleetDistinct.F()/observed))
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
