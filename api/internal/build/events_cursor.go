package build

import (
	"math"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// cursorMarks is the /v1/events cursor bound (EV-CURSOR-SETTLE-1). An
// event_id is taken when a row is inserted, and a writer's xid often comes
// earlier still, so neither id nor xid order tells whether a smaller id can
// still commit. A read can: once a later read's snapshot xmin reaches its
// xmax, every transaction running at it has ended, and no id up to the
// largest it saw can still appear. bound is the largest such id; an after
// answer passes no id above it and every cursor stops at it.
type cursorMarks struct {
	open  []store.EventsRead // reads not yet passed by the latest read's xmin, oldest first
	bound int64
	last  store.EventsRead
}

// openReads is how many reads cursorMarks keeps waiting while a transaction
// stays open; past it, the newest replaces the one before it.
const openReads = 16

// next is m after the read r: r and the reads still open whose xmax r's xmin
// reached raise the bound to their largest id. A read without a snapshot
// bounds nothing.
func (m cursorMarks) next(r store.EventsRead) cursorMarks {
	if r.Xmax == 0 {
		return cursorMarks{bound: math.MaxInt64, last: r}
	}
	if m.last.Xmax == 0 {
		m = cursorMarks{}
	}
	out := cursorMarks{bound: m.bound, last: r}
	for _, p := range append(m.open[:len(m.open):len(m.open)], r) {
		if p.Xmax <= r.Xmin {
			out.bound = max(out.bound, p.MaxID)
		} else {
			out.open = append(out.open, p)
		}
	}
	if n := len(out.open); n > openReads {
		out.open = append(out.open[:openReads-1:openReads-1], out.open[n-1])
	}
	return out
}

// Pending reports whether the window holds ids above the cursor bound: the
// server then reads the events again on its next poll, even with no new
// event, so the bound moves up once the transactions that held it end.
func (w *EventWindow) Pending() bool { return w.marks.last.MaxID > w.marks.bound }

// FollowEvents carries prev's cursor marks into res, a full build made after
// it, so a rebuild does not drop the reads that bound the cursor.
func FollowEvents(res, prev *Result) {
	res.Events.marks = prev.Events.marks.next(res.Events.marks.last)
}
