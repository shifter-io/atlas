package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

// The API's statements, by name, for `siteapi explain`: each one's call site
// and the arguments it takes, built from the same SQL the call site runs.
// Explain runs every one with EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON), in one
// read-only transaction, with arguments taken from the current build.

// The layouts a statement runs in (Statement.Only).
const (
	everyLayout = ""
	v4Layout    = "v4"
)

// Statement is one statement shape of the API's serving path.
type Statement struct {
	Name string
	Site string
	Only string
	// Query is the statement and its arguments for p.
	Query func(p *ExplainParams) (string, []any)
}

// ExplainParams are the arguments the statements take, from the current build.
type ExplainParams struct {
	Layout SiteLayout `json:"-"`
	Build  int64      `json:"build"`
	V4     bool       `json:"v4"`
	// Lists is every network list the head names; List the one with the most
	// rows, its network's provider, row count and largest dimension.
	Lists     []int64 `json:"lists"`
	List      int64   `json:"list"`
	Provider  string  `json:"provider"`
	ListRows  int     `json:"list_rows"`
	Dimension string  `json:"dimension"`
	// Places and Networks are the label ids of the dimension's rows and of
	// the city (places) and ASN (networks) rows, which row-cache misses read.
	Places   []int32 `json:"place_ids"`
	Networks []int32 `json:"asn_label_ids"`
	// LargestPlace is the dimension's place with the most IPs; Country the
	// list's country with the most IPs, City its city with the most.
	LargestPlace *Place  `json:"largest_place"`
	Country      *string `json:"country"`
	City         *string `json:"city"`
	// CursorAt and CursorID are the oldest of the EventWindow newest events:
	// where the notable read starts, and the since filter.
	CursorAt time.Time `json:"cursor_at"`
	CursorID int64     `json:"cursor_id"`
	// EventCountry and EventTarget are the newest of those events' country
	// and target, the country= and target= filters.
	EventCountry string `json:"event_country"`
	EventTarget  string `json:"event_target"`
}

func fixed(sql string) func(*ExplainParams) (string, []any) {
	return func(*ExplainParams) (string, []any) { return sql, nil }
}

// eventsShape is QueryEvents' query for the filter f makes, or its notable read.
func eventsShape(notable bool, f func(p *ExplainParams) EventFilter) func(*ExplainParams) (string, []any) {
	return func(p *ExplainParams) (string, []any) {
		filter := f(p)
		clauses, args := eventClauses(filter)
		limit, quota := readLimits(filter)
		switch {
		case notable:
			return eventsQuery(append(clauses, notableClause), args, quota)
		case filter.After != nil:
			return afterQuery(clauses, args, limit)
		}
		return eventsQuery(clauses, args, limit)
	}
}

// Statements is the registry: every statement shape the API runs, the
// registered ones (RegisterStatement) after the API's own.
func Statements() []Statement {
	return append(registry(), registered...)
}

var registered []Statement

// RegisterStatement adds a page answer's request-time statement to
// Statements, from an init() in the answer's own file
// (api/answers/README.md).
func RegisterStatement(s Statement) {
	registered = append(registered, s)
}

func registry() []Statement {
	out := []Statement{
		{"poll.site_layout", "ReadSiteLayout", everyLayout, fixed(siteLayoutSQL)},
		{"poll.fingerprint", "readFingerprint", everyLayout, func(p *ExplainParams) (string, []any) {
			revision, updated, _ := siteFingerprintSQL(p.Layout)
			return fmt.Sprintf(fingerprintSQL, revision, updated), nil
		}},
		{"load.now", "Inputs.loadHead", everyLayout, fixed(nowSQL)},
	}
	for _, r := range siteReads {
		out = append(out, Statement{"load.site." + r.table, "readSite", everyLayout, func(p *ExplainParams) (string, []any) { return r.sql(p.Layout), nil }})
	}
	return append(out,
		Statement{"load.framework_targets", "Inputs.catalogSteps", everyLayout, fixed(frameworkTargetsSQL)},
		Statement{"load.framework_providers", "Inputs.frameworkSteps", everyLayout, fixed(frameworkProvidersSQL)},
		Statement{"load.framework_products", "Inputs.frameworkSteps", everyLayout, fixed(frameworkProductsSQL)},
		Statement{"load.snapshot", "Inputs.loadSnapshot", everyLayout, fixed(snapshotSQL)},
		Statement{"load.network_counts", "NetLists.counts", v4Layout, func(p *ExplainParams) (string, []any) {
			return netCountSQL, []any{p.Lists, strings.Split(netCodes, "")}
		}},
		Statement{"netlist.rows", "NetLists.read (scanList)", v4Layout, func(p *ExplainParams) (string, []any) {
			return listQuery(true), []any{[]int64{p.List}, []string{p.Dimension}}
		}},
		Statement{"labels.places", "labels.readLabels", v4Layout, func(p *ExplainParams) (string, []any) { return placeLabelsSQL, []any{p.Places} }},
		Statement{"labels.networks", "labels.readLabels", v4Layout, func(p *ExplainParams) (string, []any) { return networkLabelsSQL, []any{p.Networks} }},
		Statement{"load.events_newest", "Inputs.loadEvents", everyLayout, func(*ExplainParams) (string, []any) { return eventsNewestSQL, []any{EventWindow + 1} }},
		Statement{"load.events_notable", "Inputs.loadEvents", everyLayout, func(p *ExplainParams) (string, []any) {
			return eventsNotableSQL, []any{p.CursorAt, p.CursorID, NotableWindow + 1}
		}},
		Statement{"events.max_id", "LoadEvents", everyLayout, fixed(maxEventSQL)},
		Statement{"events.query.default", "QueryEvents", everyLayout, eventsShape(false, func(*ExplainParams) EventFilter { return EventFilter{Limit: 100} })},
		Statement{"events.query.default_notable", "QueryEvents", everyLayout, eventsShape(true, func(*ExplainParams) EventFilter { return EventFilter{Limit: 100} })},
		Statement{"events.query.product", "QueryEvents", everyLayout, eventsShape(false, func(p *ExplainParams) EventFilter { return EventFilter{Product: p.Provider, Limit: 100} })},
		Statement{"events.query.kinds", "QueryEvents", everyLayout, eventsShape(false, func(*ExplainParams) EventFilter {
			return EventFilter{Kinds: []string{"pulse_down", "pulse_recovered"}, Limit: 100}
		})},
		Statement{"events.query.category", "QueryEvents", everyLayout, eventsShape(false, func(*ExplainParams) EventFilter { return EventFilter{Category: "incident", Limit: 100} })},
		Statement{"events.query.since", "QueryEvents", everyLayout, eventsShape(false, func(p *ExplainParams) EventFilter { return EventFilter{Since: &p.CursorAt, Limit: 100} })},
		Statement{"events.query.network", "QueryEvents", everyLayout, eventsShape(false, func(p *ExplainParams) EventFilter { return EventFilter{Provider: p.Provider, Limit: 100} })},
		Statement{"events.query.network_notable", "QueryEvents", everyLayout, eventsShape(true, func(p *ExplainParams) EventFilter { return EventFilter{Provider: p.Provider, Limit: 100} })},
		Statement{"events.query.country", "QueryEvents", everyLayout, eventsShape(false, func(p *ExplainParams) EventFilter { return EventFilter{Country: p.EventCountry, Limit: 100} })},
		Statement{"events.query.target", "QueryEvents", everyLayout, eventsShape(false, func(p *ExplainParams) EventFilter { return EventFilter{Target: p.EventTarget, Limit: 100} })},
		Statement{"events.query.proxy_kind", "QueryEvents", everyLayout, eventsShape(false, func(*ExplainParams) EventFilter { return EventFilter{ProxyKind: "residential", Limit: 100} })},
		Statement{"events.query.after", "QueryEvents", everyLayout, eventsShape(false, func(p *ExplainParams) EventFilter { return EventFilter{After: &p.CursorID, Limit: 100} })},
		Statement{"events.query.q", "QueryEvents", everyLayout, eventsShape(false, func(*ExplainParams) EventFilter { return EventFilter{Words: []string{"pulse"}, Limit: 100} })},
	)
}

// ErrWritable is Explain's refusal of a role that can write.
var ErrWritable = errors.New("explain needs a read-only role: this one can write (superuser, or INSERT, UPDATE, DELETE or TRUNCATE on a public or site table)")

const writableSQL = `SELECT rolsuper OR EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname IN ('public', 'site') AND c.relkind IN ('r', 'p') AND has_table_privilege(c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE'))
 FROM pg_catalog.pg_roles WHERE rolname = current_user`

// Explain runs every registered statement with EXPLAIN ANALYZE in one
// REPEATABLE READ read-only transaction. A statement that fails is reported
// and the others still run; a role that can write is refused.
func Explain(ctx context.Context, db TxBeginner) (*ExplainReport, error) {
	tx, err := db.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck // read-only
	var writable bool
	if err := tx.QueryRow(ctx, writableSQL).Scan(&writable); err != nil {
		return nil, fmt.Errorf("role privileges: %w", err)
	}
	if writable {
		return nil, ErrWritable
	}
	p, err := explainParams(ctx, tx)
	if err != nil {
		return nil, fmt.Errorf("parameters: %w", err)
	}
	report := &ExplainReport{Params: *p}
	for _, s := range Statements() {
		report.Statements = append(report.Statements, explainOne(ctx, tx, p, s))
	}
	return report, nil
}

// explainOne runs s in a savepoint of tx, so a failure leaves tx usable.
func explainOne(ctx context.Context, tx pgx.Tx, p *ExplainParams, s Statement) Explained {
	sql, args := s.Query(p)
	e := Explained{Name: s.Name, Site: s.Site, Only: s.Only, SQL: sql, Skipped: skipReason(p, s)}
	if e.Skipped != "" {
		return e
	}
	sp, err := tx.Begin(ctx)
	if err == nil {
		var out []byte
		if err = sp.QueryRow(ctx, "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "+sql, args...).Scan(&out); err == nil {
			err = e.read(out)
		}
		sp.Rollback(ctx) //nolint:errcheck // read-only
	}
	if err != nil {
		e.Error = err.Error()
	}
	return e
}

// skipReason is why s is not run against the current build, "" when it is.
func skipReason(p *ExplainParams, s Statement) string {
	switch {
	case s.Only == v4Layout && !p.V4:
		return "the current build is not v4"
	case s.Only == v4Layout && len(p.Lists) == 0:
		return "the current build names no network list"
	case s.Name == "labels.places" && len(p.Places) == 0, s.Name == "labels.networks" && len(p.Networks) == 0:
		return "the list's rows name no such label"
	}
	for _, r := range siteReads {
		if s.Name == "load.site."+r.table {
			return r.gap(p.Layout)
		}
	}
	return ""
}

// explainParams reads the arguments from the current build in tx.
func explainParams(ctx context.Context, tx pgx.Tx) (*ExplainParams, error) {
	p := &ExplainParams{}
	var err error
	if p.Layout, err = ReadSiteLayout(ctx, tx); err != nil {
		return nil, err
	}
	if err := p.readCursor(ctx, tx); err != nil {
		return nil, err
	}
	var s SnapshotRow
	err = tx.QueryRow(ctx, snapshotSQL).Scan(&s.BuildID, &s.ContentHash, &s.Payload, &p.V4)
	if errors.Is(err, pgx.ErrNoRows) {
		return p, nil
	}
	if err != nil {
		return nil, err
	}
	if p.Build = s.BuildID; !p.V4 {
		return p, nil
	}
	if err := p.pickList(ctx, tx, &s); err != nil || p.List == 0 {
		return p, err
	}
	return p, p.readPlaces(ctx, tx)
}

// readCursor sets the cursor to the oldest of the EventWindow newest events
// (now and 0 while there are none).
func (p *ExplainParams) readCursor(ctx context.Context, tx pgx.Tx) error {
	rows, err := collect(ctx, tx, eventsNewestSQL, ScanEvent, EventWindow)
	p.CursorAt = time.Now()
	if len(rows) > 0 {
		p.CursorAt, p.CursorID = rows[len(rows)-1].At, rows[len(rows)-1].ID
	}
	for i := len(rows) - 1; i >= 0; i-- {
		if c := rows[i].Country; c != nil {
			p.EventCountry = *c
		}
		if t := rows[i].Target; t != nil {
			p.EventTarget = *t
		}
	}
	return err
}

// pickList sets the lists s names and the one with the most rows, with its
// provider and largest dimension.
func (p *ExplainParams) pickList(ctx context.Context, tx pgx.Tx, s *SnapshotRow) error {
	nf, items, entries, err := s.networks()
	if err != nil {
		return err
	}
	provider := map[int64]string{}
	for i, e := range entries {
		var named struct{ Provider string }
		json.Unmarshal(s.Payload[nf.Start+items[i].Start:nf.Start+items[i].End], &named) //nolint:errcheck // networks() decoded it
		for _, w := range e.Windows {
			if w.Lists != nil && !slices.Contains(p.Lists, w.Lists.ID) {
				p.Lists, provider[w.Lists.ID] = append(p.Lists, w.Lists.ID), named.Provider
			}
		}
	}
	slices.Sort(p.Lists)
	held := NewNetLists(nil)
	lists, err := held.Load(ctx, tx, p.Lists)
	if err != nil {
		return err
	}
	for _, id := range p.Lists {
		l, total := lists[id], 0
		for _, n := range l.count {
			total += n
		}
		if total > p.ListRows {
			p.List, p.ListRows, p.Provider = id, total, provider[id]
			d := slices.Index(l.count[:], slices.Max(l.count[:]))
			p.Dimension = netCodes[d : d+1]
		}
	}
	return nil
}

// placeIPs is a list row's place and IPs.
type placeIPs struct {
	place *Place
	ips   int64
}

// readPlaces reads the list's country, city, ASN and largest-dimension rows:
// their label ids, the dimension's largest place, the largest country and its
// largest city.
func (p *ExplainParams) readPlaces(ctx context.Context, tx pgx.Tx) error {
	lb := labels{places: map[int32]*Place{}, networks: map[int32]*Network{}}
	var top, country placeIPs
	var cities []placeIPs
	codes := strings.Join(slices.Compact(slices.Sorted(slices.Values([]string{"a", "c", "g", p.Dimension}))), "")
	err := scanList(ctx, tx, true, 0, []int64{p.List}, codes, func(r *scannedRow) error {
		row := placeIPs{nil, r.Ips}
		if r.place >= 0 {
			row.place = lb.place(r.place)
		}
		if r.code == 'c' && row.place != nil && r.Ips > country.ips {
			country = row
		}
		if r.code == 'g' && row.place != nil {
			cities = append(cities, row)
		}
		dimension := r.code == p.Dimension[0]
		if r.place >= 0 && (dimension || r.code == 'g') && !slices.Contains(p.Places, r.place) {
			p.Places = append(p.Places, r.place)
		}
		if r.network >= 0 && (dimension || r.code == 'a') && !slices.Contains(p.Networks, r.network) {
			p.Networks = append(p.Networks, r.network)
		}
		if dimension && row.place != nil && r.Ips > top.ips {
			top = row
		}
		return nil
	})
	if err == nil {
		err = lb.readLabels(ctx, tx)
	}
	if err != nil || country.place == nil {
		p.LargestPlace = top.place
		return err
	}
	p.LargestPlace, p.Country = top.place, country.place.CountryCode
	best := placeIPs{}
	for _, c := range cities {
		if c.ips > best.ips && same(c.place.CountryCode, p.Country) {
			best = c
		}
	}
	if best.place != nil {
		p.City = best.place.City
	}
	return nil
}

func same(a, b *string) bool { return a != nil && b != nil && *a == *b }
