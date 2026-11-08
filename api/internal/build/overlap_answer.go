package build

import (
	"math"
	"net/http"
	"sort"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// /v1/overlap?kind=&range= (api/answers/overlap.md): every network of a kind
// with its pool basis, exclusivity, ownership label, likely upstream and
// every pair, and the kind's market duplication, for one range.

func init() {
	RegisterAnswer(PageAnswer{
		Name:  "overlap",
		Build: buildOverlap,
		Route: "GET /v1/overlap",
		Key:   overlapKey,
		Samples: []string{"/v1/overlap", "/v1/overlap?range=24h", "/v1/overlap?kind=residential&range=7d",
			"/v1/overlap?kind=residential&range=30d"},
	})
}

// OverlapAnswer is one kind and range of /v1/overlap.
type OverlapAnswer struct {
	GeneratedAt string           `json:"generatedAt"`
	Kind        string           `json:"kind"`
	Range       string           `json:"range"`
	Networks    []OverlapNetwork `json:"networks"`
	Market      OverlapMarket    `json:"market"`
}

// OverlapNetwork is one network's pool against the other networks of its kind.
type OverlapNetwork struct {
	Slug string `json:"slug"`
	Name string `json:"name"`
	// PoolIps is the pool the shares were computed on, named by PoolBasis.
	PoolIps        Num              `json:"poolIps"`
	PoolBasis      model.Str        `json:"poolBasis"`
	ExclusiveShare Num              `json:"exclusiveShare"`
	SharedShare    Num              `json:"sharedShare"`
	ExclusiveIps   Num              `json:"exclusiveIps"`
	Label          model.Str        `json:"label"`
	LikelyUpstream *OverlapUpstream `json:"likelyUpstream"`
	Peers          []OverlapPeer    `json:"peers"`
}

// Pool bases: where a network's poolIps comes from.
const (
	// PoolBasisOverlap: the overlap rows' ips_ours, the measured pool the
	// shares were computed on.
	PoolBasisOverlap = "overlap_ips_ours"
	// PoolBasisWindow: the window's measured pool.unique_ips, for a network
	// with no pair to read ips_ours from.
	PoolBasisWindow = "window_unique_ips"
)

const overlapDefaultKind = "residential"

func overlapPath(kind, rangeID string) string { return "/v1/overlap/" + kind + "/" + rangeID }

func overlapKey(r *http.Request) (string, error) {
	q, err := Query(r, "kind", "range")
	if err != nil {
		return "", err
	}
	kind, err := QueryKind(q, overlapDefaultKind)
	if err != nil {
		return "", err
	}
	rangeID, err := QueryRange(q)
	if err != nil {
		return "", err
	}
	return overlapPath(kind, rangeID), nil
}

// buildOverlap is every measured kind's body per range, and residential's
// always (empty without a snapshot), so the default request answers.
func buildOverlap(c *Context) (map[string]any, error) {
	kinds := map[string]bool{overlapDefaultKind: true}
	for _, e := range c.Entries {
		for k := range e.Snap {
			kinds[k] = true
		}
	}
	out := map[string]any{}
	for kind := range kinds {
		for _, r := range Ranges {
			out[overlapPath(kind, r.ID)] = OverlapFor(c, kind, r.ID)
		}
	}
	return out, nil
}

// OverlapFor is the kind's overlap in one range: every network measured in
// that kind, in catalog order, and their market.
func OverlapFor(c *Context, kind, rangeID string) *OverlapAnswer {
	ans := &OverlapAnswer{GeneratedAt: c.GeneratedAt, Kind: kind, Range: rangeID, Networks: []OverlapNetwork{}}
	byID := map[int]*entry{}
	for _, e := range c.Entries {
		if sp := e.Snap[kind]; sp != nil {
			byID[int(sp.ProductID.F())] = e
		}
	}
	pairs := kindPairs(c.Snapshot, byID, kind, rangeID)
	for _, e := range c.Entries {
		if sp := e.Snap[kind]; sp != nil {
			ans.Networks = append(ans.Networks, overlapNetwork(e, sp.Window(rangeID), pairs[int(sp.ProductID.F())]))
		}
	}
	ans.Market = MarketOverlap(ans.Networks, kindFleetDistinct(c.Snapshot, kind, rangeID))
	return ans
}

// networkPairs is one network's pairs in a range and its ips_ours.
type networkPairs struct {
	peers []OverlapPeer
	ours  Num
}

// kindPairs reads the snapshot's overlap rows of the range between two of
// the kind's networks (byID: by product id), from both sides. A v4 build's
// rows are its networks' windows[r].overlap.peers (store/network.go).
func kindPairs(s *snap.Snapshot, byID map[int]*entry, kind, rangeID string) map[int]*networkPairs {
	out := map[int]*networkPairs{}
	if s == nil {
		return out
	}
	side := func(id, other int, shared, ours, theirs Num) {
		np := out[id]
		if np == nil {
			np = &networkPairs{ours: nan}
			out[id] = np
		}
		if !np.ours.OK() {
			np.ours = ours
		}
		o := byID[other].P
		np.peers = append(np.peers, OverlapPeer{Slug: o.Slug, SharedIps: shared, ShareOfOurs: share(shared, ours),
			ShareOfTheirs: share(shared, theirs), name: o.Name})
	}
	seen := map[[2]int]bool{}
	for _, r := range s.Overlap {
		a, b := int(r.ProductA.F()), int(r.ProductB.F())
		ea, eb := byID[a], byID[b]
		pair := [2]int{min(a, b), max(a, b)}
		if r.Window != rangeID || ea == nil || eb == nil || a == b || r.Cohort != ea.Snap[kind].Cohort || seen[pair] {
			continue
		}
		seen[pair] = true
		side(a, b, r.SharedIps, r.IpsA, r.IpsB)
		side(b, a, r.SharedIps, r.IpsB, r.IpsA)
	}
	for _, np := range out {
		sort.SliceStable(np.peers, func(i, j int) bool {
			pi, pj := np.peers[i], np.peers[j]
			return pi.SharedIps > pj.SharedIps || pi.SharedIps == pj.SharedIps && pi.Slug < pj.Slug
		})
	}
	return out
}

// share is part of whole, NaN when whole is unknown or zero.
func share(part, whole Num) Num {
	if !part.OK() || !whole.OK() || whole <= 0 {
		return nan
	}
	return part / whole
}

// overlapNetwork is e's row from its window and its pairs (nil: no pair).
func overlapNetwork(e *entry, w snap.Window, np *networkPairs) OverlapNetwork {
	n := OverlapNetwork{Slug: e.P.Slug, Name: e.P.Name, ExclusiveShare: w.Overlap.ExclusiveShare,
		SharedShare: w.Overlap.SharedShare, Peers: []OverlapPeer{}, PoolIps: nan}
	switch {
	case np != nil && np.ours.OK():
		n.PoolIps, n.PoolBasis = np.ours, model.S(PoolBasisOverlap)
	case w.Pool.UniqueIps.OK():
		n.PoolIps, n.PoolBasis = w.Pool.UniqueIps, model.S(PoolBasisWindow)
	}
	if np != nil {
		n.Peers = np.peers
	}
	n.ExclusiveIps = nan
	if n.PoolIps.OK() && n.ExclusiveShare.OK() {
		n.ExclusiveIps = Num(math.Round(n.PoolIps.F() * n.ExclusiveShare.F()))
	}
	n.Label = OwnershipLabel(n.ExclusiveShare, n.SharedShare)
	n.LikelyUpstream = LikelyUpstream(n.Peers)
	return n
}
