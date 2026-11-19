package build

import (
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// The network-blocked rule (NETWORK-BLOCKED-1, api/FIELDS.md "Network-blocked
// targets"): a target is refused by the network's own gateway, as opposed to
// the target site challenging or blocking the request, when nearly every try
// of it in the range failed with a gateway refusal while the network's other
// targets did not.
const (
	// blockedMinTries is the tries the target, and the network's other
	// targets together, need in the range (the HTTP targets' scoring minimum).
	blockedMinTries = scoreMinRequests
	// blockedShare is the target's share of gateway refusals that marks it
	// refused.
	blockedShare = 0.90
	// blockedOthersShare is the share of gateway refusals under which the
	// network's other targets are not refused.
	blockedOthersShare = 0.50
)

// gatewayRefusals are the error codes of a try the gateway itself refused:
// the tunnel or connection refused (proxy_connect, which includes a 402 or
// 403 the gateway answers to CONNECT) and the credentials refused
// (proxy_auth, a 407).
var gatewayRefusals = []string{"proxy_connect", "proxy_auth"}

// refusalCounts is m's tries and gateway refusals; ok is false when m does
// not count its errors by code (a snapshot from before error categories).
func refusalCounts(m snap.Metrics) (tries, refused float64, ok bool) {
	if m.ErrorCategories == nil || !m.Completed.OK() {
		return 0, 0, false
	}
	for _, code := range gatewayRefusals {
		refused += m.ErrorCategories[code]
	}
	return m.Completed.F(), refused, true
}

// networkBlocked is target id's networkBlocked in window w: null under
// blockedMinTries tries (or without error categories), true when at least
// blockedShare of them are gateway refusals and the network's other targets
// together have blockedMinTries tries with under blockedOthersShare refused,
// null when the others are refused as well or have too few tries (a refusal
// of the whole gateway or account is not one of this target), else false.
func networkBlocked(w snap.Window, id string) model.Bool {
	tries, refused, ok := refusalCounts(w.Targets[id].Metrics)
	if !ok || tries < blockedMinTries {
		return nil
	}
	if refused/tries < blockedShare {
		return model.B(false)
	}
	others, othersRefused := otherRefusals(w, id)
	if others < blockedMinTries || othersRefused/others >= blockedOthersShare {
		return nil
	}
	return model.B(true)
}

// otherRefusals is the tries and gateway refusals of every target of w but
// id, counting each try once: a group target (its sources' tries together)
// is left out, and so are id's own sources.
func otherRefusals(w snap.Window, id string) (tries, refused float64) {
	own := map[string]bool{id: true}
	if b := w.Targets[id].Browser; b != nil {
		for _, s := range b.Sources {
			own[s] = true
		}
	}
	for other, t := range w.Targets {
		if own[other] || t.Browser != nil && len(t.Browser.Sources) > 0 {
			continue
		}
		if n, r, ok := refusalCounts(t.Metrics); ok {
			tries, refused = tries+n, refused+r
		}
	}
	return tries, refused
}
