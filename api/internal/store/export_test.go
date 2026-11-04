package store

// MergeNewest exposes mergeNewest to the external tests.
var MergeNewest = mergeNewest

// ListQuery exposes listQuery to the external tests.
var ListQuery = listQuery

// Pause makes LoadLists call f once it has read the current snapshot row,
// with the calculated_breakdowns lock on or off; undo restores both.
func Pause(f func(), lock bool) (undo func()) {
	afterSnapshot, lockBreakdowns = f, lock
	return func() { afterSnapshot, lockBreakdowns = nil, true }
}

// FailedRead leaves c as a list read that failed after scanning a row of
// place id leaves it: the place created and pending, the lists dropped.
func FailedRead(c *Lists, place int32) {
	c.places, c.networks = map[int32]*Place{}, map[int32]*Network{}
	c.place(place)
	c.held = nil
}
