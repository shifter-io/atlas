package store

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strconv"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/jackc/pgx/v5"
)

// Payload v4 (proxy-index calculated contract K9) adds to the head one
// summary per network, "networks": a (provider, kind) over all its product
// groups, with the keys of a v3 product group, cohort "network" and, per
// window, the K10 network list it names ("lists": {"id", "entries"}) and its
// overlap with the other networks of its kind ("overlap.peers"). A v4 build
// is read as the head and those lists only: no product part, no group list,
// and the head without its per-cohort overlap and fleet rows.
//
// So that the existing build serves it, the reader gives each network as one
// product group of the payload the build decodes (Full), and every one of its
// windows:
//   - breakdowns.country and pool.countries_breakdown: every country row of
//     its list; pool.asns_breakdown: every ASN row with IPs, most first (as a
//     v3 window restores it whole);
//   - breakdown_entries: its list's row counts;
//   - the snapshot's overlap rows the build reads, made from overlap.peers,
//     with the network's cohort.
// A network with exactly one requested country gets it as criteria.country,
// as the group it summarizes has it. Every window's compact list is
// SnapshotRow.Networks: the build reads the region, city, ASN and City x ASN
// rows from there, each when it needs them. The country and ASN arrays are the
// only rows the reader writes out, and Full is held only while a build is
// made: the API's own state is the head and the lists' row counts (NetLists).

// networkHeadSQL is a v4 head (s.payload) as text, without "parts", without
// "overlap" (its rows are per cohort; a network's are its overlap.peers) and
// with only the every-group ("*") rows of "fleet": what proxy-index
// calculated/api_head.sql stores as api_head when a build is published.
const networkHeadSQL = `((s.payload - 'parts' - 'overlap' - 'fleet') || CASE WHEN s.payload ? 'fleet' THEN jsonb_build_object('fleet',
 (SELECT coalesce(jsonb_agg(f ORDER BY i), '[]'::jsonb) FROM jsonb_array_elements(s.payload->'fleet') WITH ORDINALITY e(f, i)
  WHERE f->>'cohort' = '*')) ELSE '{}'::jsonb END)::text`

// NetworkCohort is the cohort of a v4 network entry.
const NetworkCohort = "network"

// networkEntry is what the reader reads of a network entry.
type networkEntry struct {
	ProductID int64  `json:"product_id"`
	Cohort    string `json:"cohort"`
	Criteria  struct {
		RequestedCountries []string `json:"requested_countries"`
	} `json:"criteria"`
	Windows map[string]networkWindow `json:"windows"`
}

type networkWindow struct {
	Period struct {
		Hours int64 `json:"hours"`
	} `json:"period"`
	Overlap struct {
		Peers []snap.OverlapPeer `json:"peers"`
	} `json:"overlap"`
	Lists *struct {
		ID int64 `json:"id"`
	} `json:"lists"`
}

// networks is the head's networks member and its entries, raw and read.
func (s *SnapshotRow) networks() (field, []field, []networkEntry, error) {
	fields, err := objectFields(s.Payload)
	if err != nil {
		return field{}, nil, nil, fmt.Errorf("snapshot %d head: %w", s.BuildID, err)
	}
	nf, ok := member(fields, "networks")
	if !ok {
		return field{}, nil, nil, fmt.Errorf("snapshot %d head: no networks", s.BuildID)
	}
	items, err := arrayItems(s.Payload[nf.Start:nf.End])
	if err != nil {
		return field{}, nil, nil, fmt.Errorf("snapshot %d networks: %w", s.BuildID, err)
	}
	entries := make([]networkEntry, len(items))
	for i, it := range items {
		if err := json.Unmarshal(s.Payload[nf.Start+it.Start:nf.Start+it.End], &entries[i]); err != nil {
			return field{}, nil, nil, fmt.Errorf("snapshot %d network %d: %w", s.BuildID, i, err)
		}
	}
	return nf, items, entries, nil
}

// readNetworks reads a v4 build's network lists into cache (nil: a fresh
// one), drops the group lists it held, and sets Full, Networks and NetRows
// (every row of those lists, netrows.go). The lists read their rows through
// db afterwards.
func (s *SnapshotRow) readNetworks(ctx context.Context, tx pgx.Tx, cache *Lists, db TxBeginner) error {
	if cache == nil {
		cache = NewLists()
	}
	net := cache.networkLists(db)
	return s.setNetworks(ctx, tx, func(ids []int64) (map[int64]*NetList, error) {
		lists, err := net.Load(ctx, tx, ids)
		if err == nil {
			s.NetRows, err = net.readAll(ctx, tx, lists, NetReadParallelRows)
		}
		return lists, err
	})
}

// networkLists is the cache's network lists reading through db, made on
// first use or when db changes; it drops the group lists the cache holds (a
// v4 build reads none).
func (c *Lists) networkLists(db TxBeginner) *NetLists {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.held, c.missing = nil, nil
	if c.net == nil || c.net.db != db {
		c.net = NewNetLists(db)
	}
	return c.net
}

// SetNetworks sets Full and Networks of a v4 snapshot whose Payload is its head,
// from the network lists by id: each network becomes one product group (see
// above). A window naming a list not in lists keeps only what the head has.
func (s *SnapshotRow) SetNetworks(lists map[int64]*NetList) error {
	return s.setNetworks(context.Background(), nil, func([]int64) (map[int64]*NetList, error) { return lists, nil })
}

// setNetworks is SetNetworks with the lists load gives for the ids the
// networks' windows name, their country and ASN rows read in tx (nil: in
// transactions of their own).
func (s *SnapshotRow) setNetworks(ctx context.Context, tx pgx.Tx, load func(ids []int64) (map[int64]*NetList, error)) error {
	nf, items, entries, err := s.networks()
	if err != nil {
		return err
	}
	ids := []int64{}
	for _, e := range entries {
		for _, w := range e.Windows {
			if w.Lists != nil && !slices.Contains(ids, w.Lists.ID) {
				ids = append(ids, w.Lists.ID)
			}
		}
	}
	lists, err := load(ids)
	if err != nil {
		return fmt.Errorf("snapshot %d network lists: %w", s.BuildID, err)
	}
	ctx = WithNetRows(ctx, s.NetRows)
	s.Networks = map[PlaceWindow]*NetList{}
	networks := s.Payload[nf.Start:nf.End]
	groups := make([][]byte, len(items))
	overlap := overlapRows{seen: map[overlapPair]bool{}}
	for i, it := range items {
		e := &entries[i]
		if groups[i], err = s.networkGroup(ctx, tx, networks[it.Start:it.End], e, lists); err != nil {
			return fmt.Errorf("snapshot %d network %d: %w", s.BuildID, e.ProductID, err)
		}
		overlap.add(e)
	}
	// The build decodes no "networks" key: it stays in the payload it reads.
	full, err := setMember(s.Payload, "overlap", overlap.json())
	if err == nil {
		full, err = withProducts(full, func(out *bytes.Buffer) error {
			out.Write(bytes.Join(groups, []byte(", ")))
			return nil
		})
	}
	if err != nil {
		return fmt.Errorf("snapshot %d: %w", s.BuildID, err)
	}
	s.full = full
	return nil
}

// networkGroup is a network entry as a product group: its single requested
// country as criteria.country and every window with its list's rows.
func (s *SnapshotRow) networkGroup(ctx context.Context, tx pgx.Tx, raw []byte, e *networkEntry, lists map[int64]*NetList) ([]byte, error) {
	fields, err := objectFields(raw)
	if err == nil && len(e.Criteria.RequestedCountries) == 1 {
		// The entry has a criteria object: it decoded its requested countries.
		var criteria []byte
		criteria, err = setMember(valueOf(raw, fields, "criteria"), "country", appendString(nil, e.Criteria.RequestedCountries[0]))
		if err == nil {
			raw, err = setMember(raw, "criteria", criteria)
		}
		if err == nil {
			fields, err = objectFields(raw)
		}
	}
	if err != nil {
		return nil, err
	}
	wf, ok := member(fields, "windows")
	if !ok {
		return raw, nil
	}
	wfields, err := objectFields(raw[wf.Start:wf.End])
	if err != nil {
		return nil, fmt.Errorf("windows: %w", err)
	}
	windows := make([][]byte, len(wfields))
	for j, f := range wfields {
		w := e.Windows[f.Key]
		if w.Lists == nil || lists[w.Lists.ID] == nil {
			continue
		}
		l := lists[w.Lists.ID]
		s.Networks[PlaceWindow{e.ProductID, e.Cohort, w.Period.Hours}] = l
		if windows[j], err = listWindow(ctx, tx, raw[wf.Start+f.Start:wf.Start+f.End], l); err != nil {
			return nil, fmt.Errorf("window %s: %w", f.Key, err)
		}
	}
	if w := rebuild(raw[wf.Start:wf.End], wfields, windows); w != nil {
		raw = splice(raw, wf, w)
	}
	return raw, nil
}

// listWindow is a network window (an object: the entry decoded it) with the
// arrays the build reads from its list l: breakdowns.country,
// breakdown_entries and the pool's country and ASN lists.
func listWindow(ctx context.Context, tx pgx.Tx, raw []byte, l *NetList) ([]byte, error) {
	head, err := l.headRows(ctx, tx)
	if err != nil {
		return nil, err
	}
	fields, err := objectFields(raw)
	pool := []byte(valueOf(raw, fields, "pool"))
	if pool == nil {
		pool = []byte("{}")
	}
	breakdowns, entries := []byte("{}"), []byte("{}")
	set := func(obj *[]byte, key string, v []byte) {
		if err == nil {
			*obj, err = setMember(*obj, key, v)
		}
	}
	for _, name := range breakdownNames {
		if strings.IndexByte(netCodes, dimensionCodes[name]) >= 0 {
			set(&entries, name, strconv.AppendInt(nil, int64(l.Len(name)), 10))
		}
	}
	set(&breakdowns, "country", rowsJSON(rowColumns['c'], pointers(head.Rows("country"))))
	set(&pool, "countries_breakdown", rowsJSON(poolColumns["countries_breakdown"], poolRows("countries_breakdown", head)))
	set(&pool, "asns_breakdown", rowsJSON(poolColumns["asns_breakdown"], poolRows("asns_breakdown", head)))
	set(&raw, "breakdowns", breakdowns)
	set(&raw, "breakdown_entries", entries)
	set(&raw, "pool", pool)
	return raw, err
}

// headRows is l's country and ASN rows (ctx's NetRowSet's, else read in tx when not cached) as a
// typed list, for the arrays listWindow writes; observations, challenges,
// blocks, p90 and p99 are NULL: a compact row does not keep them, and no
// answer shows them.
func (l *NetList) headRows(ctx context.Context, tx pgx.Tx) (*List, error) {
	out := &List{rows: map[byte][]ListRow{}}
	for _, name := range []string{"country", "asn"} {
		v, err := rowsFrom(ctx, tx, l, name)
		if err != nil {
			return nil, err
		}
		rows := make([]ListRow, v.Len())
		for i := range rows {
			r := v.Row(i)
			rows[i] = ListRow{Place: r.Place, Network: r.Network, Ips: r.Ips, Observations: -1, Completed: r.Completed, Successes: r.Successes,
				Challenges: -1, Blocks: -1, P50: r.P50, P90: -1, P99: -1, position: int32(i)}
		}
		out.rows[dimensionCodes[name]] = rows
	}
	return out, nil
}

// overlapPair is one pair of networks in one window, the lower product id first.
type overlapPair struct {
	window string
	a, b   int64
}

// overlapRows are the snapshot overlap rows made from the networks' peers,
// one per pair and window (the build reads a row from either side).
type overlapRows struct {
	rows []snap.OverlapRow
	seen map[overlapPair]bool
}

// add adds e's peers of every window, in window label order.
func (o *overlapRows) add(e *networkEntry) {
	labels := make([]string, 0, len(e.Windows))
	for label := range e.Windows {
		labels = append(labels, label)
	}
	slices.Sort(labels)
	for _, label := range labels {
		for _, p := range e.Windows[label].Overlap.Peers {
			if !p.ProductID.OK() {
				continue
			}
			peer := int64(p.ProductID)
			pair := overlapPair{label, min(e.ProductID, peer), max(e.ProductID, peer)}
			if o.seen[pair] {
				continue
			}
			o.seen[pair] = true
			o.rows = append(o.rows, snap.OverlapRow{Cohort: e.Cohort, Window: label, ProductA: snap.Num(e.ProductID), ProductB: p.ProductID,
				SharedIps: p.SharedIps, IpsA: p.IpsOurs, IpsB: p.IpsTheirs})
		}
	}
}

// json is the rows as the payload's overlap array.
func (o *overlapRows) json() []byte {
	if len(o.rows) == 0 {
		return []byte("[]")
	}
	b, _ := json.Marshal(o.rows) // snap.OverlapRow holds strings and numbers only
	return b
}

// NewNetList is a network list of the rows given per payload breakdown name
// (country, region, city, asn, city_asn), in position order, with names of
// its own: a list as NetLists.Load reads it, for tests and synthetic builds.
func NewNetList(rows map[string][]NetRow) (*NetList, error) {
	n := NewNetLists(nil)
	l := &NetList{}
	places, networks := map[*Place]int32{}, map[*Network]int32{}
	for name, rs := range rows {
		d := strings.IndexByte(netCodes, dimensionCodes[name])
		if d < 0 {
			return nil, fmt.Errorf("a network list has no %q rows", name)
		}
		for _, r := range rs {
			c := netRow{p50: r.P50, place: -1, network: -1}
			var err error
			for _, v := range []struct {
				to   *uint32
				from int64
			}{{&c.ips, r.Ips}, {&c.completed, r.Completed}, {&c.successes, r.Successes}} {
				if *v.to, err = counter(v.from); err != nil {
					return nil, err
				}
			}
			if r.Place != nil {
				c.place = nameIndex(places, r.Place, &n.names.places, &n.names.placeText)
			}
			if r.Network != nil {
				c.network = nameIndex(networks, r.Network, &n.names.networks, &n.names.networkText)
			}
			l.rows[d] = append(l.rows[d], c)
		}
		l.count[d] = len(l.rows[d])
	}
	n.fillTexts([2]int{})
	l.names = n.names
	return l, nil
}

// nameIndex is the index of name in names, appended (with an empty search
// text) when new.
func nameIndex[T any](at map[*T]int32, name *T, names *[]*T, texts *[]string) int32 {
	i, ok := at[name]
	if !ok {
		i = int32(len(*names))
		at[name] = i
		*names, *texts = append(*names, name), append(*texts, "")
	}
	return i
}
