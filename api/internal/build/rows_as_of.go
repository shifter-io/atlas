package build

import "github.com/Zeus-Development/atlas/api/internal/snap"

// rowsAsOf is the time window w's place rows are counted to: its list's
// cutoff where the calculation kept the list of an earlier build
// (breakdowns_as_of: a census-pulse group's 7d or 30d list, rebuilt every 6
// hours, CALC-SCALE-4), else the window's own end.
func rowsAsOf(w snap.Window) string {
	if w.BreakdownsAsOf != "" {
		return snap.ISOText(w.BreakdownsAsOf)
	}
	return snap.ISOText(w.Period.End)
}

// oldestRowsAsOf is the oldest rowsAsOf of the groups' windows of the range
// ("" without a group).
func oldestRowsAsOf(groups []*snap.Product, rangeID string) string {
	oldest := ""
	for _, p := range groups {
		if at := rowsAsOf(p.Window(rangeID)); oldest == "" || at < oldest {
			oldest = at
		}
	}
	return oldest
}

// setRowsAsOf sets each range's oldest rowsAsOf over every product group of
// the snapshot: the place answers that merge every network's rows.
func (pg *Pages) setRowsAsOf(s *snap.Snapshot) {
	groups := []*snap.Product{}
	for i := 0; s != nil && i < len(s.Products); i++ {
		groups = append(groups, &s.Products[i])
	}
	pg.asOf = map[string]string{}
	for _, r := range Ranges {
		pg.asOf[r.ID] = oldestRowsAsOf(groups, r.ID)
	}
}

// listAsOf is the breakdownAsOf of a list of scope: its network's range
// (scope slug/kind), else every network's.
func (pg *Pages) listAsOf(scope, rangeID string) string {
	if n := pg.Network[scope]; n != nil && n.Ranges[rangeID] != nil {
		return n.Ranges[rangeID].BreakdownAsOf
	}
	return pg.asOf[rangeID]
}
