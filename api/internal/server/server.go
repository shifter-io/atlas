// Package server serves the precomputed responses and keeps them fresh.
//
// A Version is an immutable set of rendered bodies built from one database
// read. Requests only ever look up bytes in the current Version (an atomic
// pointer), so they never touch the database; the refresh loop builds the
// next Version in the background and swaps it in. A failed build leaves the
// previous Version serving and is reported by /healthz, except a current
// build older than payload v4 (store.ErrBeforeV4): nothing serves then, and
// /healthz gives BeforeV4Reason.
package server

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/memtrace"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"golang.org/x/sync/errgroup"
)

// Version is one immutable generation of responses.
type Version struct {
	ID          uint64
	Fingerprint string
	BuiltAt     time.Time
	BuildTime   time.Duration
	// Phases of BuildTime: database read, assembly, rendering and compression.
	LoadTime, AssembleTime, RenderTime time.Duration
	Static                             map[string]*Rendered
	Result                             *build.Result
	search                             *LRU
	events                             *LRU
	pages                              *LRU
	// Dataset is the dataset file on disk (datasetfile.go), named by its
	// hash, DatasetPath (/v1/files/dataset-<sha256>.json), cached for a year;
	// /v1/dataset redirects to it. Nil when the Version has none (a
	// RenderVersion without Options.FilesDir).
	Dataset     *DatasetFile
	DatasetPath string
	// NextData is when the next data update is due (the published snapshot's
	// cutoff plus Options.DataPeriod, on its grid); zero when unknown. Data
	// answers are cached until then (dataCache).
	NextData time.Time
	// coldLists is what the warm step left cold (/healthz coldLists).
	coldLists ColdLists
}

// BeforeV4Reason is /healthz's reason (an unavailable_reason code) while the
// current build is older than payload v4, which the API does not serve.
const BeforeV4Reason = build.ReasonBeforeV4

// Source loads inputs; the database in production, fixtures in tests.
type Source interface {
	Fingerprint(ctx context.Context) (store.Fingerprint, error)
	Load(ctx context.Context) (*store.Inputs, error)
	// LoadEvents reads only the events and the newest event id (store.LoadEvents).
	LoadEvents(ctx context.Context) (*store.Inputs, error)
	Events(ctx context.Context, f store.EventFilter) ([]store.Event, error)
}

// Options configure a Server.
type Options struct {
	Interval  time.Duration
	SearchLRU int
	EventsLRU int
	Logger    *slog.Logger
	// DataPeriod is the data update schedule (default DefaultDataPeriod) and
	// CacheFloor the TTL when the next update is unknown or overdue (default
	// DefaultCacheFloor). Now is the clock (default time.Now).
	DataPeriod time.Duration
	CacheFloor time.Duration
	Now        func() time.Time
	// FilesDir is the directory the dataset file is written to (siteapi
	// serve -files-dir). Refresh needs it.
	FilesDir string
	// CodeID identifies the code that encodes the dataset file (default:
	// CodeID(BinaryInfo())). A restart reuses a file only when it was written
	// by the same, known code.
	CodeID string
}

// Server holds the current Version and refresh state.
type Server struct {
	src       Source
	opts      Options
	current   atomic.Pointer[Version]
	refreshMu sync.Mutex
	nextID    atomic.Uint64
	files     *datasetFiles
	state     struct {
		sync.Mutex
		lastError   string
		lastErrorAt time.Time
		lastCheck   time.Time
		// reason is why nothing serves (BeforeV4Reason), empty otherwise.
		reason   string
		builds   int
		failures int
		// unserved is the serving version's pages that a published product
		// should have and that do not answer 200, and unservedMore the count
		// beyond them (unservedPages).
		unserved     []Unserved
		unservedMore int
		// siteGaps is the last build's missing site tables, logged when
		// they change (so once, not every rebuild).
		siteGaps []string
		// failed is the last failed full build (retry.go), nil after a
		// success or while the serving Version answers the fingerprint.
		failed *failedBuild
	}
	log *slog.Logger
}

// New creates a server; call Refresh before serving.
func New(src Source, opts Options) *Server {
	if opts.Interval <= 0 {
		opts.Interval = 10 * time.Second
	}
	if opts.SearchLRU <= 0 {
		opts.SearchLRU = 1024
	}
	if opts.EventsLRU <= 0 {
		opts.EventsLRU = 256
	}
	if opts.Logger == nil {
		opts.Logger = slog.Default()
	}
	if opts.DataPeriod <= 0 {
		opts.DataPeriod = DefaultDataPeriod
	}
	if opts.CacheFloor <= 0 {
		opts.CacheFloor = DefaultCacheFloor
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.CodeID == "" {
		opts.CodeID = CodeID(BinaryInfo())
	}
	s := &Server{src: src, opts: opts, log: opts.Logger}
	if opts.FilesDir != "" {
		s.files = newDatasetFiles(opts.FilesDir, opts.CodeID, opts.Logger)
	}
	return s
}

// Current returns the serving Version (nil before the first build).
func (s *Server) Current() *Version { return s.current.Load() }

// Refresh rebuilds when the fingerprint's data part changed (or force), and
// only refreshes the events answers when nothing but the newest event id did
// (refreshEvents). A rebuild writes a new dataset file only when its marker
// changed (datasetfile.go). Concurrent calls are single-flight: one build at a
// time, and a caller that waited re-checks the fingerprint instead of building
// the same thing twice. A fingerprint whose build failed is not built again
// unless force: never while refused (Refused), else after its wait (retry.go).
// Every call also drops the replaced dataset files whose grace ended. When
// ctx ends during a rebuild (a shutdown), the rebuild stops within seconds
// and is discarded: the serving Version stays, and nothing records a failure.
func (s *Server) Refresh(ctx context.Context, force bool) (bool, error) {
	s.refreshMu.Lock()
	defer s.refreshMu.Unlock()
	fp, err := s.fingerprint(ctx)
	if err != nil {
		return false, err
	}
	cur := s.current.Load()
	switch change(cur, fp, force) {
	case changeNone:
		s.forgetFailed()
		return false, nil
	case changeEvents:
		s.forgetFailed()
		return s.refreshEvents(ctx, cur)
	}
	if !force && s.held(fp) {
		return false, nil
	}
	v, err := s.rebuild(ctx, cur)
	if err != nil && ctx.Err() != nil {
		s.log.Info("rebuild stopped; serving the previous version", "reason", context.Cause(ctx))
		return false, err
	}
	if err != nil {
		s.fail(err)
		s.buildFailed(fp, err)
		if errors.Is(err, store.ErrBeforeV4) {
			s.current.Store(nil)
			s.state.Lock()
			s.state.reason = BeforeV4Reason
			s.state.Unlock()
		}
		return false, err
	}
	s.current.Store(v)
	memtrace.Mark(ctx, "serving")
	s.rebuilt(v)
	return true, nil
}

// fingerprint drops the dataset files whose grace ended and reads the
// source's fingerprint, recording the check; a failure is recorded too.
func (s *Server) fingerprint(ctx context.Context) (store.Fingerprint, error) {
	if s.files == nil {
		err := errors.New("no files directory (Options.FilesDir) for the dataset file")
		s.fail(err)
		return store.Fingerprint{}, err
	}
	s.files.expire(s.opts.Now())
	rctx, cancel := context.WithTimeout(ctx, ReadTimeout)
	defer cancel()
	fp, err := s.src.Fingerprint(rctx)
	s.state.Lock()
	s.state.lastCheck = time.Now()
	s.state.Unlock()
	if err != nil {
		s.fail(fmt.Errorf("fingerprint: %w", err))
	}
	return fp, err
}

// rebuild builds a new Version from prev and gives it its dataset file.
func (s *Server) rebuild(ctx context.Context, prev *Version) (*Version, error) {
	start := time.Now()
	v, err := s.build(ctx, prev)
	if err != nil {
		return nil, err
	}
	memtrace.Mark(ctx, "dataset file")
	f, err := s.files.ensure(ctx, v.Result.Dataset, datasetMarker(v.Result.Fingerprint), s.opts.Now())
	if err == nil {
		err = setDataset(v, f)
	}
	if err != nil {
		return nil, err
	}
	v.BuildTime = time.Since(start)
	return v, nil
}

// rebuilt records and logs the Version that now serves, with the pages it
// does not serve (unservedPages) and its site gaps when they changed.
func (s *Server) rebuilt(v *Version) {
	unserved, more := s.unservedPages(v)
	s.state.Lock()
	s.state.builds++
	s.state.lastError, s.state.reason, s.state.failed = "", "", nil
	s.state.unserved, s.state.unservedMore = unserved, more
	gapsChanged := !slices.Equal(s.state.siteGaps, v.Result.SiteGaps)
	s.state.siteGaps = v.Result.SiteGaps
	s.state.Unlock()
	if gapsChanged && len(v.Result.SiteGaps) > 0 {
		s.log.Warn("site tables missing; serving them empty", "version", v.ID, "gaps", v.Result.SiteGaps)
	}
	s.log.Info("cache rebuilt", "version", v.ID, "build_ms", v.BuildTime.Milliseconds(), "load_ms", v.LoadTime.Milliseconds(),
		"assemble_ms", v.AssembleTime.Milliseconds(), "render_ms", v.RenderTime.Milliseconds(), "providers", len(v.Result.Dataset.Providers), "dataset_bytes", v.Dataset.Bytes)
	if len(unserved) > 0 {
		s.log.Warn("published products without their network pages", "version", v.ID, "pages", unserved, "more", more,
			"unbuilt", v.Result.Pages.Unbuilt, "unbuilt_events", v.Result.Events.Unbuilt)
	}
}

func (s *Server) fail(err error) {
	s.state.Lock()
	s.state.lastError = err.Error()
	s.state.lastErrorAt = time.Now()
	s.state.failures++
	s.state.Unlock()
	msg := "cache refresh failed; serving the previous version"
	if errors.Is(err, store.ErrBeforeV4) {
		msg = "cache refresh failed; serving nothing: calculation older than payload v4: recalculate"
	}
	s.log.Error(msg, "error", err)
}

// build reads and renders a new Version, reusing prev's bodies whose bytes
// did not change (reuse.go), and puts its network list rows in the row cache
// (warm): the serving Version is swapped only after.
func (s *Server) build(ctx context.Context, prev *Version) (*Version, error) {
	t0 := time.Now()
	memtrace.Mark(ctx, "load")
	rctx, cancel := context.WithTimeout(ctx, ReadTimeout)
	in, err := s.src.Load(rctx)
	cancel()
	if err != nil {
		return nil, fmt.Errorf("load: %w", err)
	}
	t1 := time.Now()
	memtrace.Mark(ctx, "assemble")
	res, err := build.BuildContext(ctx, in)
	if err != nil {
		return nil, &Refused{fmt.Errorf("build: %w", err)}
	}
	if prev != nil {
		build.FollowEvents(res, prev.Result)
	}
	t2 := time.Now()
	memtrace.Mark(ctx, "render and compression")
	v, err := renderVersion(ctx, s.nextID.Add(1), res, s.opts, prev)
	if err != nil {
		return nil, err
	}
	v.LoadTime, v.AssembleTime, v.RenderTime = t1.Sub(t0), t2.Sub(t1), time.Since(t2)
	if in.Snapshot != nil {
		memtrace.Mark(ctx, "row cache warm")
		v.coldLists = s.warmed(v.ID, in.Snapshot.NetRows.Warm())
	}
	return v, nil
}

// RenderVersion pre-renders every static response of a build and, with
// opts.FilesDir, writes its dataset file there (always: no meta is read).
func RenderVersion(id uint64, res *build.Result, opts Options) (*Version, error) {
	v, err := renderVersion(context.Background(), id, res, opts, nil)
	if err != nil || opts.FilesDir == "" {
		return v, err
	}
	f, err := newDatasetFiles(opts.FilesDir, "", slog.Default()).write(context.Background(), res.Dataset)
	if err != nil {
		return nil, err
	}
	return v, setDataset(v, f)
}

// renderVersion renders res's static responses; it stops with ctx's error
// once ctx ends.
func renderVersion(ctx context.Context, id uint64, res *build.Result, opts Options, prev *Version) (*Version, error) {
	v := &Version{ID: id, Fingerprint: res.Fingerprint.Key(), BuiltAt: res.BuiltAt, Result: res, Static: map[string]*Rendered{},
		search: NewLRU(max(opts.SearchLRU, 1)), events: NewLRU(max(opts.EventsLRU, 1)), pages: NewLRU(PagesLRU),
		NextData: nextData(res.Dataset.Snapshot.AsOf, opts.DataPeriod)}
	type job struct {
		path  string
		value any
		cache string
	}
	jobs := []job{
		{"/v1/search-index", res.Search, CacheData},
		{"/v1/coverage", res.Coverage.Index(), CacheData},
	}
	for _, p := range res.Coverage.Providers {
		jobs = append(jobs, job{"/v1/coverage/" + p.ID, p, CacheData})
	}
	for path, value := range pageAnswers(res.Pages) {
		jobs = append(jobs, job{path, value, CacheData})
	}
	reused := reusable(prev)
	rendered := make([]*Rendered, len(jobs))
	var g errgroup.Group
	g.SetLimit(RenderWorkers())
	for i, j := range jobs {
		g.Go(func() error {
			if err := ctx.Err(); err != nil {
				return err
			}
			r, err := reused.render(http.StatusOK, j.value, j.cache)
			if err != nil {
				return fmt.Errorf("render %s: %w", j.path, err)
			}
			rendered[i] = r
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return nil, err
	}
	for i, j := range jobs {
		v.Static[j.path] = rendered[i]
	}
	shareIdentical(v.Static)
	if err := registeredFit(v); err != nil {
		return nil, err
	}
	if err := setDataset(v, nil); err != nil {
		return nil, err
	}
	if err := addDefaultEvents(v); err != nil {
		return nil, err
	}
	return v, nil
}

// setDataset names v's dataset file (nil: none) and renders /v1/version,
// what a browser polls to learn that the data changed. The revision is the
// dataset file's own hash, so it changes exactly when the published file
// does, and a site built from a `siteapi dump` file can compute it from the
// bytes. No extra database work: it is rendered once per Version.
func setDataset(v *Version, f *DatasetFile) error {
	v.Dataset, v.DatasetPath = f, ""
	if f != nil {
		v.DatasetPath = f.Path()
	}
	ver, err := Render(http.StatusOK, VersionInfo{Revision: Revision(v), BuiltAt: v.Result.BuiltAt.UTC(), Contract: build.Contract}, CacheVersion, LevelFast)
	if err != nil {
		return err
	}
	v.Static[versionPath] = ver
	return nil
}

// VersionInfo is /v1/version.
type VersionInfo struct {
	Revision string    `json:"revision"`
	BuiltAt  time.Time `json:"builtAt"`
	// Contract is the date of the API's last breaking change (build.Contract,
	// api/CHANGES.md).
	Contract string `json:"contract"`
}

// Revision identifies the published data of a Version: the hex SHA-256 of the
// dataset file, which is also its ETag and part of its name.
func Revision(v *Version) string {
	if v.Dataset == nil {
		return ""
	}
	return v.Dataset.SHA256
}

// ReadTimeout bounds each database read of a refresh (the fingerprint, the
// load, the events). The build after the load has no deadline: only the end
// of Refresh's context (a shutdown) stops it.
const ReadTimeout = 2 * time.Minute

// Run polls for changes until the context ends, and returns once a rebuild in
// progress has stopped. A tick that is ready together with the end of the
// context starts no Refresh: select picks either case at random.
func (s *Server) Run(ctx context.Context) {
	t := time.NewTicker(s.opts.Interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if ctx.Err() != nil {
				return
			}
			_, _ = s.Refresh(ctx, false)
		}
	}
}

// Handler routes the API.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	static := func(path string) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) { s.serveStatic(w, r, path) }
	}
	mux.HandleFunc("GET /v1/dataset", s.serveDataset)
	mux.HandleFunc("GET /v1/files/{name}", s.serveFile)
	for _, p := range []string{"/v1/search-index", "/v1/coverage", versionPath} {
		mux.HandleFunc("GET "+p, static(p))
	}
	mux.HandleFunc("GET /v1/coverage/{slug}", func(w http.ResponseWriter, r *http.Request) {
		s.serveStatic(w, r, "/v1/coverage/"+r.PathValue("slug"))
	})
	mux.HandleFunc("GET /v1/search", s.serveSearch)
	s.pageRoutes(mux)
	mux.HandleFunc("GET /v1/events", s.serveEvents)
	mux.HandleFunc("GET /healthz", s.serveHealth)
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			w.Header().Set("Allow", "GET, HEAD")
			writeError(w, http.StatusMethodNotAllowed, build.MethodNotAllowed(r.Method))
			return
		}
		notFound(w, r)
	})
	return mux
}

// write sends a rendered body with conditional-request and coding support.
func (s *Server) write(w http.ResponseWriter, r *http.Request, v *Version, body *Rendered) {
	enc := Negotiate(r.Header.Get("Accept-Encoding"))
	data, coding, etag, length := body.Variant(enc)
	h := w.Header()
	h.Set("Vary", "Accept-Encoding")
	h.Set("ETag", etag)
	h.Set("Cache-Control", body.CacheControl)
	if body.CacheControl == CacheData {
		s.setDataCache(h, v)
	}
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("Access-Control-Allow-Origin", "*")
	if v != nil {
		h.Set("X-Site-Version", strconv.FormatUint(v.ID, 10))
	}
	if body.Status == http.StatusOK && body.Matches(r.Header.Get("If-None-Match")) {
		w.WriteHeader(http.StatusNotModified)
		return
	}
	h.Set("Content-Type", "application/json; charset=utf-8")
	if coding != "" {
		h.Set("Content-Encoding", coding)
	}
	h.Set("Content-Length", length)
	w.WriteHeader(body.Status)
	if r.Method != http.MethodHead {
		w.Write(data) //nolint:errcheck
	}
}

func (s *Server) serveStatic(w http.ResponseWriter, r *http.Request, path string) {
	v := s.current.Load()
	if v == nil {
		s.unavailable(w)
		return
	}
	body, ok := v.Static[path]
	if !ok {
		notFound(w, r)
		return
	}
	s.write(w, r, v, body)
}

func (s *Server) serveSearch(w http.ResponseWriter, r *http.Request) {
	v := s.current.Load()
	if v == nil {
		s.unavailable(w)
		return
	}
	for k := range r.URL.Query() {
		if k != "q" {
			badRequest(w, build.UnknownParameter(k))
			return
		}
	}
	q := r.URL.Query().Get("q")
	if len(q) > maxQuery {
		badRequest(w, build.TooLong("q", maxQuery))
		return
	}
	key := strings.Join(build.Tokens(q), " ")
	body, ok := v.search.Get(key)
	if !ok {
		var err error
		body, err = Render(http.StatusOK, v.Result.Search.Search(key), CacheData, LevelFast)
		if err != nil {
			s.renderFailed(w, r, err)
			return
		}
		v.search.Add(key, body)
	}
	s.write(w, r, v, body)
}

// MaxEvents is the framework's /v1/events limit.
const MaxEvents = 500

var categories = []string{"census", "pulse", "incident", "audit"}

// ParseEventFilter validates /v1/events parameters like the framework does,
// and the live tape's: network (a site slug, mapped by provider to its
// framework provider), country, target, q, proxyKind, after and before.
func ParseEventFilter(q map[string][]string, provider func(slug string) (string, bool)) (store.EventFilter, error) {
	f := store.EventFilter{Limit: 100}
	get := func(k string) string {
		if v := q[k]; len(v) > 0 {
			return v[0]
		}
		return ""
	}
	for k := range q {
		switch k {
		case "product", "kind", "category", "since", "limit", "network", "country", "target", "q", "proxyKind", "after", "before":
		default:
			return f, build.UnknownParameter(k)
		}
	}
	if l := get("limit"); l != "" {
		n, err := build.IntParam("limit", l, 1, MaxEvents)
		if err != nil {
			return f, err
		}
		f.Limit = n
	}
	f.Product = get("product")
	for _, k := range strings.Split(get("kind"), ",") {
		if k != "" {
			f.Kinds = append(f.Kinds, k)
		}
	}
	if c := get("category"); c != "" {
		if !slices.Contains(categories, c) {
			return f, build.InvalidValue("category", c, categories...)
		}
		f.Category = c
	}
	if since := get("since"); since != "" {
		t, err := parseSince(since)
		if err != nil {
			return f, err
		}
		t = t.UTC()
		f.Since = &t
	}
	return f, parseTapeFilters(&f, get, provider)
}

func parseSince(raw string) (time.Time, error) {
	// URL decoding turns the "+" of "+00:00" into a space.
	s := strings.Replace(strings.TrimSpace(raw), " ", "+", 1)
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05.999999999", "2006-01-02"} {
		if t, err := time.Parse(layout, s); err == nil {
			return t, nil
		}
	}
	return time.Time{}, build.InvalidFormat("since", raw, build.FormatISOTime)
}

func eventsBody(res *build.Result, f store.EventFilter) (*build.EventsAnswer, error) {
	out, ok := res.Events.Answer(f)
	if !ok {
		return nil, errNeedDB
	}
	return out, nil
}

var errNeedDB = errors.New("events window does not cover this filter")

func (s *Server) serveEvents(w http.ResponseWriter, r *http.Request) {
	v := s.current.Load()
	if v == nil {
		s.unavailable(w)
		return
	}
	f, err := ParseEventFilter(r.URL.Query(), v.Result.Events.Provider)
	if err != nil {
		badRequest(w, err)
		return
	}
	key := eventsKey(f)
	body, ok := v.events.Get(key)
	if !ok {
		resp, err := eventsBody(v.Result, f)
		if errors.Is(err, errNeedDB) {
			ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
			rows, qerr := s.src.Events(ctx, f)
			cancel()
			if errors.Is(qerr, store.ErrUnknownCursor) {
				badRequest(w, build.UnknownCursor("before", strconv.FormatInt(*f.Before, 10)))
				return
			}
			if qerr != nil {
				writeError(w, http.StatusServiceUnavailable, build.Unavailable(build.ReasonEventsQueryFails))
				s.log.Error("events fallback query failed", "error", qerr)
				return
			}
			resp = v.Result.Events.FromRows(f, rows)
		} else if err != nil {
			s.renderFailed(w, r, err)
			return
		}
		body, err = Render(http.StatusOK, resp, CacheEvents, LevelFast)
		if err != nil {
			s.renderFailed(w, r, err)
			return
		}
		v.events.Add(key, body)
	}
	s.write(w, r, v, body)
}
