package build_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"os"
	"slices"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/server"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
	"github.com/jackc/pgx/v5"
)

// The case's networks: the every-request fixture's group published by three
// providers, one group each, in one cohort, with overlap rows between them.
var (
	netProviders = []string{"shifter", "acme", "zeta"}
	netLabels    = []string{"24h", "7d", "30d"}
	netCodes     = map[string]string{"c": "country", "r": "region", "g": "city", "a": "asn", "y": "city_asn"}
)

const fixtureCohort = "7a5d84127c244707dc0e"

// statementLog records every statement a connection runs, the list ids
// of its list row reads and those whose network row counts it reads.
type statementLog struct {
	mu              sync.Mutex
	sql             []string
	listIDs, counts []int64
}

// networkCountsSQL is the store's statement reading network lists' row
// counts: the lists a v4 load reads (store.NetLists).
var networkCountsSQL = func() string {
	for _, s := range store.Statements() {
		if s.Name == "load.network_counts" {
			sql, _ := s.Query(&store.ExplainParams{})
			return sql
		}
	}
	return ""
}()

func (s *statementLog) TraceQueryStart(ctx context.Context, _ *pgx.Conn, d pgx.TraceQueryStartData) context.Context {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sql = append(s.sql, d.SQL)
	if strings.Contains(d.SQL, "calculated_list_rows") && len(d.Args) > 0 {
		if ids, ok := d.Args[0].([]int64); ok {
			s.listIDs = append(s.listIDs, ids...)
			if d.SQL == networkCountsSQL {
				s.counts = append(s.counts, ids...)
			}
		}
	}
	return ctx
}

func (s *statementLog) TraceQueryEnd(context.Context, *pgx.Conn, pgx.TraceQueryEndData) {}

func (s *statementLog) reads(table string) bool {
	return slices.ContainsFunc(s.sql, func(q string) bool { return strings.Contains(q, table) })
}

// loadLogged loads and builds the current build over one logged connection.
func loadLogged(t *testing.T, db *testdb.DB, cache *store.Lists) (*build.Result, *statementLog) {
	t.Helper()
	ctx := context.Background()
	cfg, err := pgx.ParseConfig(db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	log := &statementLog{}
	cfg.Tracer = log
	conn, err := pgx.ConnectConfig(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close(ctx)
	in, err := store.LoadLists(ctx, conn, cache)
	if err != nil {
		t.Fatal(err)
	}
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	return res, log
}

// netCase is the case's lists, by provider and window: each group's (v3)
// and its network's (v4), with the same rows.
type netCase struct {
	groupLists, netLists map[string]map[string]int64
	counts               map[string]map[string]int // network list rows by breakdown name, per window
}

func writeNetLists(t *testing.T, c *pgx.Conn) netCase {
	t.Helper()
	nc := netCase{map[string]map[string]int64{}, map[string]map[string]int64{}, map[string]map[string]int{}}
	for i, p := range netProviders {
		nc.groupLists[p], nc.netLists[p] = map[string]int64{}, map[string]int64{}
		for k, label := range netLabels {
			seed := 3*i + k
			nc.groupLists[p][label], nc.netLists[p][label] = writeList(t, c, seed), writeList(t, c, seed)
			counts := map[string]int{}
			for code, name := range netCodes {
				_, counts[name] = oracleList(t, c, nc.netLists[p][label], code, 0, false)
			}
			nc.counts[label] = counts
		}
	}
	return nc
}

// v3Group is the fixture's group as provider p's (product id pid) in v3
// list form, its windows naming the lists.
func v3Group(t *testing.T, c *pgx.Conn, template map[string]any, p string, pid int, lists map[string]int64) map[string]any {
	t.Helper()
	g := cloneMap(t, template)
	g["provider"], g["name"], g["product_id"] = p, strings.ToUpper(p[:1])+p[1:], pid
	g["id"] = fmt.Sprintf("%d:%s", pid, fixtureCohort)
	for label, entry := range g["windows"].(map[string]any) {
		w, id := entry.(map[string]any), lists[label]
		pool := w["pool"].(map[string]any)
		entries, poolEntries, kept := map[string]any{}, map[string]any{}, map[string]any{}
		for code, name := range listNames {
			_, n := oracleList(t, c, id, code, 0, false)
			entries[name], kept[name] = n, min(n, keep[name])
		}
		for name, code := range map[string]string{"geo": "g", "countries_breakdown": "c", "asns_breakdown": "a"} {
			_, n := oracleList(t, c, id, code, 0, true)
			poolEntries[name], kept[name] = n, min(n, keep[name])
			delete(pool, name)
		}
		delete(w, "breakdowns")
		w["breakdown_entries"], pool["list_entries"], w["lists"] = entries, poolEntries, map[string]any{"id": id, "entries": kept}
	}
	return g
}

func cloneMap(t *testing.T, v map[string]any) map[string]any {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

// netOverlap is the overlap of products a < b in a window: distinct shares.
func netOverlap(a, b int, label string) snap.OverlapRow {
	w := map[string]int{"24h": 1, "7d": 2, "30d": 3}[label]
	return snap.OverlapRow{Cohort: fixtureCohort, Window: label, ProductA: snap.Num(a), ProductB: snap.Num(b),
		SharedIps: snap.Num(100*a + 10*b + w), IpsA: snap.Num(1000 + a), IpsB: snap.Num(1000 + b)}
}

// netPayloads is the case as a v4 head (testscale.NetworkHead, naming the
// case's network lists) with its groups (product parts), made from the data
// with per-cohort overlap and fleet rows and an every-group fleet entry.
func netPayloads(t *testing.T, c *pgx.Conn, nc netCase) (v4 []byte, groups [][]byte) {
	t.Helper()
	raw, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	template := doc["products"].([]any)[0].(map[string]any)
	overlap, products := []any{}, []any{}
	for i, p := range netProviders {
		g := v3Group(t, c, template, p, i+1, nc.groupLists[p])
		b, err := json.Marshal(g)
		if err != nil {
			t.Fatal(err)
		}
		groups, products = append(groups, b), append(products, g)
		for j := i + 1; j < len(netProviders); j++ {
			for _, label := range netLabels {
				overlap = append(overlap, netOverlap(i+1, j+1, label))
			}
		}
	}
	for _, f := range doc["fleet"].([]any) {
		all := cloneMap(t, f.(map[string]any))
		all["cohort"] = snap.AllCohorts
		doc["fleet"] = append(doc["fleet"].([]any), all)
	}
	delete(doc, "products")
	delete(doc, "runs")
	doc["overlap"], doc["parts"], doc["schema_version"] = overlap, map[string]int{"products": len(groups), "runs": 0}, 3
	doc["products"] = products
	whole, err := json.Marshal(doc)
	if err == nil {
		v4, err = testscale.NetworkHead(&store.SnapshotRow{Payload: whole}, 0, func(w testscale.NetWindow, _ map[string][]store.NetRow) (int64, map[string]int, error) {
			return nc.netLists[w.Provider][w.Label], nc.counts[w.Label], nil
		})
	}
	if err != nil {
		t.Fatal(err)
	}
	return v4, groups
}

// netAnswers is every answer of a build as the server renders them, and
// pages and searches of every place list of its networks, of all networks
// and of its countries.
func netAnswers(t *testing.T, res *build.Result) map[string][]byte {
	t.Helper()
	res.BuiltAt = time.Date(2026, 11, 5, 0, 0, 0, 0, time.UTC)
	res.Coverage.BuiltAt = build.JSISO(res.BuiltAt)
	v, err := server.RenderVersion(1, res, server.Options{FilesDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	out := map[string][]byte{}
	for p, r := range v.Static {
		if p != "/v1/version" && !registered(p) {
			out[p] = r.Identity
		}
	}
	for name, part := range map[string]any{"dataset": res.Dataset, "details": res.Details, "pages": res.Pages} {
		if out[name], err = server.Marshal(part); err != nil {
			t.Fatal(err)
		}
	}
	out["dataset"], out["details"] = withoutV4Dataset(t, out["dataset"]), withoutV4Dataset(t, out["details"])
	scopes := map[string][]string{build.AllNetworks + "/" + build.AllNetworks: {"country", "region", "city", "asn"}}
	for _, p := range netProviders {
		scopes[p+"/residential"] = scopes[build.AllNetworks+"/"+build.AllNetworks]
	}
	for cc := range res.Pages.Country {
		scopes["country/"+cc] = []string{"city"}
	}
	for scope, dims := range scopes {
		for _, r := range netLabels {
			for _, d := range dims {
				for _, q := range []struct {
					page  int
					query string
				}{{1, ""}, {2, ""}, {3, ""}, {1, "de"}, {1, "be Berlin"}, {1, "telekom"}, {1, "as3209"}, {1, "zz"}, {1, "zyx"}, {2, "tail"}} {
					page, _, _ := res.Pages.List(context.Background(), "", scope, r, d, q.query, q.page)
					if out[fmt.Sprintf("places %s %s %s %d %q", scope, r, d, q.page, q.query)], err = json.Marshal(page); err != nil {
						t.Fatal(err)
					}
				}
			}
		}
	}
	return out
}

// oneGroupGolden is the golden of TestANetworkOfOneGroupAnswersAsTheGroup:
// the answers of the v3 build of its data, but for the documented id, cohort
// and schema version strings (TestTheNetworkGoldensAreTheV3Answers).
const oneGroupGolden = "network-one-group"

// TestANetworkOfOneGroupAnswersAsTheGroup is regression and equality cases
// 1 and 2 (API-SUM-1): a v4 build whose networks have one product group each
// gives every answer, byte for byte, the golden holds (the v3 build of the
// same data's, but for the documented id, cohort and schema version
// strings), and its City x ASN lists are that build's coverage evidence; it
// reads neither product parts nor group lists, only its network lists.
func TestANetworkOfOneGroupAnswersAsTheGroup(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	c := db.Conn(t)
	defer c.Close(context.Background())
	nc := writeNetLists(t, c)
	v4, groups := netPayloads(t, c, nc)
	cache := store.NewLists()
	db.PublishParts(t, v4, groups, nil)
	db.Exec(t, "UPDATE calculated_snapshots SET content_hash = 'same'")
	fromV4, log4 := loadLogged(t, db, cache)
	if read := slices.Compact(slices.Sorted(slices.Values(log4.listIDs))); log4.reads("calculated_snapshot_parts") ||
		!subset(log4.listIDs, nc.netLists) || len(read) != 9 || cache.Fetched != 0 {
		t.Errorf("the v4 build: parts read %v, list ids %v (want the 9 network lists only), %d group lists fetched",
			log4.reads("calculated_snapshot_parts"), log4.listIDs, cache.Fetched)
	}
	golden(t, oneGroupGolden, oneGroupAnswers(t, fromV4))
}

// oneGroupAnswers is a v4 build's answers (netAnswers) and its City x ASN
// lists (cityAsnLists).
// registered reports whether path is a registered answer's (api/answers/):
// those have golden files of their own (answers-v4-scale/<name>.json), so the
// goldens of the answers from before the registry leave them out.
func registered(path string) bool {
	for _, a := range build.Answers() {
		if a.Owns(path) {
			return true
		}
	}
	return false
}

func oneGroupAnswers(t *testing.T, res *build.Result) map[string][]byte {
	t.Helper()
	out := netAnswers(t, res)
	maps.Copy(out, cityAsnLists(t, res))
	return out
}

func compareAnswers(t *testing.T, name string, want, got map[string][]byte) {
	t.Helper()
	keys := make([]string, 0, len(want))
	for k := range want {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		if bytes.Equal(want[k], got[k]) {
			continue
		}
		var diffs []string
		diff(generic(t, want[k]), generic(t, got[k]), k, &diffs)
		t.Errorf("%s %s differs (%d bytes, %d bytes):\n%s", name, k, len(want[k]), len(got[k]), strings.Join(diffs, "\n"))
	}
	if len(got) != len(want) {
		t.Errorf("%s: %d answers, want %d", name, len(got), len(want))
	}
}

// subset reports whether every id is one of lists' ids.
func subset(ids []int64, lists map[string]map[string]int64) bool {
	for _, id := range ids {
		found := false
		for _, byLabel := range lists {
			for _, l := range byLabel {
				found = found || l == id
			}
		}
		if !found {
			return false
		}
	}
	return len(ids) > 0
}

// withoutV4Dataset is a dataset (or the details) without what a v4 build's
// dataset file holds differently by design (API-DATASET-2): its lists, its
// country pages and the providers' coverage evidence. checkCoverageRows and
// the dataset file tests check those.
func withoutV4Dataset(t *testing.T, b []byte) []byte {
	t.Helper()
	g, ok := generic(t, b).(map[string]any)
	if !ok {
		t.Fatalf("not an object: %.80s", b)
	}
	delete(g, "lists")
	if pages, ok := g["pages"].(map[string]any); ok {
		for k := range pages {
			if strings.HasPrefix(k, "/v1/countries") {
				delete(pages, k)
			}
		}
	}
	providers, _ := g["providers"].([]any)
	for _, d := range g {
		if detail, ok := d.(map[string]any); ok && detail["provider"] != nil {
			providers = append(providers, detail["provider"])
		}
	}
	for _, p := range providers {
		delete(p.(map[string]any), "coverageEvidence")
	}
	out, err := json.Marshal(g)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// cityAsnLists is a v4 build's coverage evidence: the dataset file's City x
// ASN list of each network, kind and range, by "city_asn <slug> <kind>
// <range>", as its row count and first page, each row with the evidence's
// fields (the list adds the country latency rank and peers).
func cityAsnLists(t testing.TB, res *build.Result) map[string][]byte {
	t.Helper()
	var doc struct {
		Lists map[string]map[string]map[string]map[string]struct {
			Total int                 `json:"total"`
			Rows  [][]json.RawMessage `json:"rows"`
		} `json:"lists"`
	}
	b, err := server.Marshal(res.Dataset)
	if err == nil {
		err = json.Unmarshal(b, &doc)
	}
	if err != nil {
		t.Fatal(err)
	}
	out := map[string][]byte{}
	for slug, kinds := range doc.Lists {
		for kind, ranges := range kinds {
			for rangeID, lists := range ranges {
				l, ok := lists["city_asn"]
				if !ok {
					continue
				}
				var rows []string
				for _, r := range l.Rows {
					rows = append(rows, string(must(json.Marshal(r[:min(len(r), 9)]))))
				}
				out[fmt.Sprintf("city_asn %s %s %s", slug, kind, rangeID)] = coverageAnswer(l.Total, rows)
			}
		}
	}
	return out
}

// coverageAnswer is a City x ASN list's row count and first page.
func coverageAnswer(total int, rows []string) []byte {
	return fmt.Appendf(nil, `{"total":%d,"rows":[%s]}`, total, strings.Join(rows, ","))
}

func must(b []byte, err error) []byte {
	if err != nil {
		panic(err)
	}
	return b
}
