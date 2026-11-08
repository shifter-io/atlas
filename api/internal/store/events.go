package store

import (
	"cmp"
	"context"
	"encoding/json"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

// EventFilter mirrors the framework's GET /v1/events parameters, plus the
// live tape's filters and its id cursor.
type EventFilter struct {
	Product  string // "provider" or "provider/product"
	Kinds    []string
	Category string
	Since    *time.Time // exclusive
	Limit    int
	// Provider is the framework provider of network=<slug>; ProxyKind the
	// product name (residential, datacenter, mobile, isp).
	Provider, Country, Target, ProxyKind string
	// After asks for the rows with a larger event_id, oldest first.
	After *int64
	// Words are q's lower-case words. They match the rendered text, so Match
	// does not read them (build.EventWindow does).
	Words []string
}

// TextScan is how many rows passing the other filters the database fallback
// reads for a q filter, which it matches outside SQL.
const TextScan = EventWindow

// Category is detect.category: the live tape tab of an event kind. The daily
// test and hour estimate kinds read as census rows; latency_rise is an incident.
func Category(kind string) string {
	switch {
	case strings.HasPrefix(kind, "census_"), kind == "daily_test_started",
		kind == "daily_test_finished", kind == "estimate_hour_released":
		return "census"
	case strings.HasPrefix(kind, "pulse_"):
		return "pulse"
	case kind == "new_hosting_asn" || kind == "ip_overlap":
		return "audit"
	}
	return "incident"
}

// Match reports whether an event passes the filter (everything but Limit).
func (f EventFilter) Match(e *Event) bool {
	if f.Product != "" {
		provider, name, hasName := strings.Cut(f.Product, "/")
		if e.Provider == nil || *e.Provider != provider {
			return false
		}
		if hasName && name != "" && (e.Product == nil || *e.Product != name) {
			return false
		}
	}
	if len(f.Kinds) > 0 {
		ok := false
		for _, k := range f.Kinds {
			if k == e.Kind {
				ok = true
				break
			}
		}
		if !ok {
			return false
		}
	}
	if f.Category != "" && Category(e.Kind) != f.Category {
		return false
	}
	if f.Since != nil && !e.At.After(*f.Since) {
		return false
	}
	if f.After != nil && e.ID <= *f.After {
		return false
	}
	return f.matchColumns(e)
}

func (f EventFilter) matchColumns(e *Event) bool {
	is := func(want string, got *string) bool { return want == "" || (got != nil && *got == want) }
	return is(f.Provider, e.Provider) && is(f.Country, e.Country) && is(f.Target, e.Target) && is(f.ProxyKind, e.Product)
}

// Routine reports whether a kind is a routine milestone: the census tab (census_*,
// the daily test and the hourly estimate releases) and pulse_finished. They come in
// bursts (an hour's release writes one row per network, a catch-up after a gap many
// hours of them), so the live tape's lists keep a share for the other kinds: pulse
// down and recovered, incidents such as latency_rise and outages, and audits.
func Routine(kind string) bool {
	return Category(kind) == "census" || kind == "pulse_finished"
}

// routineSQL is Routine in SQL for the row alias e.
const routineSQL = `(left(e.kind,7)='census_' OR e.kind IN
 ('daily_test_started','daily_test_finished','estimate_hour_released','pulse_finished'))`

// afterOrder is the order of an after read: oldest id first.
const afterOrder = ` ORDER BY e.event_id LIMIT `

// notableClause keeps the non-routine rows of the fallback's notable read.
const notableClause = "NOT " + routineSQL

// Prioritized reports whether f is answered with Prioritize: it names no kind,
// no tab and no cursor, as the live tape's first read does. A filter that names
// kinds or a tab gets exactly the newest matching rows, and a client catching up
// with since or after gets every row in order.
func (f EventFilter) Prioritized() bool {
	return len(f.Kinds) == 0 && f.Category == "" && f.Since == nil && f.After == nil
}

// NotableQuota is how many of a prioritized answer's `limit` rows the newest
// non-routine rows may take before routine rows (half, rounded up).
func NotableQuota(limit int) int { return (limit + 1) / 2 }

// Prioritize picks at most `limit` of n rows held newest first (kind(i) is row i's
// kind) and returns their indices, newest first: the newest non-routine rows up to
// NotableQuota(limit), then the newest remaining rows of any kind. So routine rows
// never push the newest notable ones off a page, and notable rows never take more
// than half of it while routine rows are waiting. The rows must hold the newest
// `limit` rows of the answer and its newest NotableQuota(limit) non-routine rows.
func Prioritize(n, limit int, kind func(int) string) []int {
	take := make([]bool, n)
	picked := 0
	for i := 0; i < n && picked < NotableQuota(limit); i++ {
		if !Routine(kind(i)) {
			take[i] = true
			picked++
		}
	}
	for i := 0; i < n && picked < limit; i++ {
		if !take[i] {
			take[i] = true
			picked++
		}
	}
	out := make([]int, 0, picked)
	for i, ok := range take {
		if ok {
			out = append(out, i)
		}
	}
	return out
}

// Pick answers f from rows held newest first: the newest f.Limit matching rows, or
// for a prioritized filter the Prioritize choice among them (see its precondition).
func Pick(rows []Event, f EventFilter) []Event {
	matched := []Event{}
	for i := range rows {
		if f.Match(&rows[i]) {
			matched = append(matched, rows[i])
		}
	}
	if !f.Prioritized() {
		return matched[:min(len(matched), f.Limit)]
	}
	out := []Event{}
	for _, i := range Prioritize(len(matched), f.Limit, func(i int) string { return matched[i].Kind }) {
		out = append(out, matched[i])
	}
	return out
}

// Newer is the events' order, newest first: time, then id.
func Newer(a, b *Event) bool {
	if !a.At.Equal(b.At) {
		return a.At.After(b.At)
	}
	return a.ID > b.ID
}

// mergeNewest merges two lists held newest first, dropping rows present in both.
func mergeNewest(a, b []Event) []Event {
	out := make([]Event, 0, len(a)+len(b))
	for len(a) > 0 || len(b) > 0 {
		switch {
		case len(b) == 0 || (len(a) > 0 && Newer(&a[0], &b[0])):
			out, a = append(out, a[0]), a[1:]
		case len(a) == 0 || Newer(&b[0], &a[0]):
			out, b = append(out, b[0]), b[1:]
		default: // the same row
			out, a, b = append(out, a[0]), a[1:], b[1:]
		}
	}
	return out
}

// QueryEvents is the database fallback when the in-memory window cannot
// answer a filter on its own (read-only pool). A prioritized filter reads the
// newest rows and the newest non-routine rows, and picks among both. With
// after it reads the Limit+1 oldest rows past it by id (the extra one tells
// whether more follow); with q, TextScan rows instead of Limit, unpicked, for
// the caller to match the words and pick.
func QueryEvents(ctx context.Context, db interface {
	BeginTx(ctx context.Context, opts pgx.TxOptions) (pgx.Tx, error)
}, f EventFilter) ([]Event, error) {
	tx, err := db.BeginTx(ctx, pgx.TxOptions{AccessMode: pgx.ReadOnly})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	clauses, args := eventClauses(f)
	limit, quota := readLimits(f)
	if f.After != nil {
		sql, args := afterQuery(clauses, args, limit)
		return collect(ctx, tx, sql, ScanEvent, args...)
	}
	rows, err := newestEvents(ctx, tx, clauses, args, limit)
	if err != nil || !f.Prioritized() {
		return pickUnlessWords(rows, f), err
	}
	notable, err := newestEvents(ctx, tx, append(clauses, notableClause), args, quota)
	if err != nil {
		return nil, err
	}
	return pickUnlessWords(mergeNewest(rows, notable), f), nil
}

// readLimits is how many rows the fallback reads: the newest (or with after
// the oldest past it) and the newest non-routine ones.
func readLimits(f EventFilter) (rows, notable int) {
	switch {
	case len(f.Words) > 0:
		return TextScan, TextScan
	case f.After != nil:
		return f.Limit + 1, 0
	}
	return f.Limit, NotableQuota(f.Limit)
}

// pickUnlessWords is Pick, or for a q filter the rows unpicked: Pick cannot
// read the rendered text.
func pickUnlessWords(rows []Event, f EventFilter) []Event {
	if len(f.Words) > 0 {
		return rows
	}
	return Pick(rows, f)
}

func eventClauses(f EventFilter) (clauses []string, args []any) {
	add := func(sql string, v any) {
		args = append(args, v)
		clauses = append(clauses, strings.ReplaceAll(sql, "?", "$"+itoa(len(args))))
	}
	if f.Product != "" {
		provider, name, _ := strings.Cut(f.Product, "/")
		add("p.provider=?", provider)
		if name != "" {
			add("p.name=?", name)
		}
	}
	if len(f.Kinds) > 0 {
		add("e.kind=ANY(?)", f.Kinds)
	}
	if f.Category != "" {
		add(`CASE WHEN left(e.kind,7)='census_'
 OR e.kind IN ('daily_test_started','daily_test_finished','estimate_hour_released') THEN 'census'
 WHEN left(e.kind,6)='pulse_' THEN 'pulse'
 WHEN e.kind IN ('new_hosting_asn','ip_overlap') THEN 'audit' ELSE 'incident' END=?`, f.Category)
	}
	if f.Since != nil {
		add("e.at>?", *f.Since)
	}
	for _, c := range []struct{ sql, v string }{{"p.provider=?", f.Provider}, {"e.country_code=?", f.Country},
		{"e.target_id=?", f.Target}, {"p.name=?", f.ProxyKind}} {
		if c.v != "" {
			add(c.sql, c.v)
		}
	}
	if f.After != nil {
		add("e.event_id>?", *f.After)
	}
	return clauses, args
}

func newestEvents(ctx context.Context, tx pgx.Tx, clauses []string, args []any, limit int) ([]Event, error) {
	sql, args := eventsQuery(clauses, args, limit)
	return collect(ctx, tx, sql, ScanEvent, args...)
}

// eventsQuery is the fallback query of the newest limit rows passing every
// clause, and its arguments.
func eventsQuery(clauses []string, args []any, limit int) (string, []any) {
	where := ""
	if len(clauses) > 0 {
		where = " WHERE " + strings.Join(clauses, " AND ")
	}
	args = append(args[:len(args):len(args)], limit)
	return "SELECT " + EventColumns + where + eventsOrder + "$" + itoa(len(args)), args
}

// afterQuery is the fallback query of the limit oldest rows by id passing
// every clause (one of them e.event_id > after), and its arguments.
func afterQuery(clauses []string, args []any, limit int) (string, []any) {
	sql, args := eventsQuery(clauses, args, limit)
	return strings.Replace(sql, eventsOrder, afterOrder, 1), args
}

func itoa(n int) string { return strconv.Itoa(n) }

// MinEventID is the smallest id of rows (0 for none).
func MinEventID(rows []Event) int64 {
	if len(rows) == 0 {
		return 0
	}
	return slices.MinFunc(rows, func(a, b Event) int { return cmp.Compare(a.ID, b.ID) }).ID
}

// EventsRead is what one events read saw (EV-CURSOR-SETTLE-1): its
// snapshot's xmin and xmax, as xid8, and the largest event_id (0 for none).
// Once a later read's xmin reaches Xmax, every transaction running at this
// read has ended, so no id up to MaxID can still appear: the /v1/events
// cursor may pass them (build.EventWindow). A zero Xmax is a read without a
// snapshot (a source other than Postgres), which bounds nothing.
type EventsRead struct{ MaxID, Xmin, Xmax int64 }

// eventsReadSQL reads EventsRead from the statement's snapshot: in Load's and
// LoadEvents' REPEATABLE READ transaction, the one their events are read in.
const eventsReadSQL = `SELECT coalesce((SELECT max(event_id) FROM public.test_events), 0),
 pg_snapshot_xmin(pg_current_snapshot())::text::bigint, pg_snapshot_xmax(pg_current_snapshot())::text::bigint`

// EventJSON is detect.event_json: the shape of /v1/events rows and of the
// snapshot's `events` (src/lib/measured-types.ts TestEvent).
type EventJSON struct {
	ID       int64           `json:"id"`
	At       string          `json:"at"`
	Kind     string          `json:"kind"`
	Category string          `json:"category"`
	Product  string          `json:"product"`
	Provider string          `json:"provider"`
	Country  *string         `json:"country"`
	Region   *string         `json:"region"`
	Target   *string         `json:"target"`
	Signal   int16           `json:"signal"`
	Severity string          `json:"severity"`
	Data     json.RawMessage `json:"data"`
}

// PyTime is Python's datetime.isoformat(timespec="microseconds") in UTC.
func PyTime(t time.Time) string {
	return t.UTC().Format("2006-01-02T15:04:05.000000") + "+00:00"
}

// JSON renders the event like the framework does.
func (e *Event) JSON() EventJSON {
	provider, product := "", ""
	if e.Provider != nil {
		provider = *e.Provider
		if e.Product != nil {
			product = provider + "/" + *e.Product
		} else {
			product = provider + "/"
		}
	}
	data := e.Data
	if len(data) == 0 {
		data = json.RawMessage("{}")
	}
	return EventJSON{ID: e.ID, At: PyTime(e.At), Kind: e.Kind, Category: Category(e.Kind), Product: product,
		Provider: provider, Country: e.Country, Region: e.Region, Target: e.Target, Signal: e.Signal,
		Severity: e.Severity, Data: data}
}
