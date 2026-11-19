package build

import (
	"cmp"
	"math"
	"slices"

	"github.com/shifter-io/atlas/api/internal/store"
)

// cursorMarks is the /v1/events cursor bound (EV-CURSOR-SETTLE-1). An
// event_id is taken when a row is inserted, and a writer's xid often comes
// earlier still, so neither id nor xid order tells whether a smaller id can
// still commit. A read can: once a later read's snapshot xmin reaches its
// Next, every transaction that had an xid at it has ended, and no id up to
// the largest it saw can still appear. bound is the largest such id; an
// after answer passes no id above it and every cursor stops at it. known is
// false until some read has been passed (after a restart).
type cursorMarks struct {
	open  []store.EventsRead // reads not yet passed by the latest read's xmin, oldest first
	bound int64
	known bool
	last  store.EventsRead
}

// openReads is how many reads cursorMarks keeps waiting while a transaction
// stays open; past it, the newest replaces the one before it.
const openReads = 16

// next is m after the read r: r and the reads still open whose Next r's xmin
// reached raise the bound to their largest id. A read without a snapshot
// bounds nothing.
func (m cursorMarks) next(r store.EventsRead) cursorMarks {
	if r.Next == 0 {
		return cursorMarks{bound: math.MaxInt64, known: true, last: r}
	}
	if m.last.Next == 0 {
		m = cursorMarks{}
	}
	out := cursorMarks{bound: m.bound, known: m.known, last: r}
	for _, p := range append(m.open[:len(m.open):len(m.open)], r) {
		if p.Next <= r.Xmin {
			out.bound, out.known = max(out.bound, p.MaxID), true
		} else {
			out.open = append(out.open, p)
		}
	}
	if n := len(out.open); n > openReads {
		out.open = append(out.open[:openReads-1:openReads-1], out.open[n-1])
	}
	return out
}

// Pending reports whether the window holds ids above the cursor bound (0
// while none is known): the server then reads the events again on its next
// poll, even with no new event, so the bound moves up once the transactions
// that held it end.
func (w *EventWindow) Pending() bool { return w.marks.last.MaxID > w.marks.bound }

// FollowEvents carries prev's cursor marks into res, a full build made after
// it, so a rebuild does not drop the reads that bound the cursor.
func FollowEvents(res, prev *Result) {
	res.Events.marks = prev.Events.marks.next(res.Events.marks.last)
}

// capCursor caps a cursor of an answer without after at the bound; while no
// bound is known yet (after a restart), at the smallest id the answer
// returns minus one, so a client's next after poll does not start from the
// oldest event. The rows above it may come again: clients deduplicate by id.
func (w *EventWindow) capCursor(a *EventsAnswer) {
	if a.Cursor == nil || *a.Cursor <= w.marks.bound {
		return
	}
	c := w.marks.bound
	if !w.marks.known {
		c = slices.MinFunc(a.Events, func(x, y EventRow) int { return cmp.Compare(x.ID, y.ID) }).ID - 1
	}
	a.Cursor = &c
}
