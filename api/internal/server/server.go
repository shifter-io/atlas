// Package server serves the precomputed responses and keeps them fresh.
//
// A Version is an immutable set of rendered bodies built from one database
// read. Requests only ever look up bytes in the current Version (an atomic
// pointer), so they never touch the database; the refresh loop builds the
// next Version in the background and swaps it in. A failed build leaves the
// previous Version serving and is reported by /healthz.
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
	"github.com/Zeus-Development/proxy-index/api/internal/store"
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
}

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
		builds      int
		failures    int
		// unserved is the serving version's pages that a published product
		// should have and that do not answer 200 (unservedPages).
		unserved []string
		// siteGaps is the last build's missing site tables, logged when
		// they change (so once, not every rebuild).
		siteGaps []string
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
// the same thing twice. Every call also drops the replaced dataset files whose
// grace ended.
func (s *Server) Refresh(ctx context.Context, force bool) (bool, error) {
	s.refreshMu.Lock()
	defer s.refreshMu.Unlock()
	if s.files == nil {
		err := errors.New("no files directory (Options.FilesDir) for the dataset file")
		s.fail(err)
		return false, err
	}
	s.files.expire(s.opts.Now())
	fp, err := s.src.Fingerprint(ctx)
	s.state.Lock()
	s.state.lastCheck = time.Now()
	s.state.Unlock()
	if err != nil {
		s.fail(fmt.Errorf("fingerprint: %w", err))
		return false, err
	}
	cur := s.current.Load()
	switch change(cur, fp, force) {
	case changeNone:
		return false, nil
	case changeEvents:
		return s.refreshEvents(ctx, cur)
	}
	start := time.Now()
	v, err := s.build(ctx, cur)
	if err != nil {
		s.fail(err)
		return false, err
	}
	f, err := s.files.ensure(v.Result.Dataset, datasetMarker(v.Result.Fingerprint), s.opts.Now())
	if err == nil {
		err = setDataset(v, f)
	}
	if err != nil {
		s.fail(err)
		return false, err
	}
	v.BuildTime = time.Since(start)
	s.current.Store(v)
	unserved := s.unservedPages(v)
	s.state.Lock()
	s.state.builds++
	s.state.lastError = ""
	s.state.unserved = unserved
	gapsChanged := !slices.Equal(s.state.siteGaps, v.Result.SiteGaps)
	s.state.siteGaps = v.Result.SiteGaps
	s.state.Unlock()
	if gapsChanged && len(v.Result.SiteGaps) > 0 {
		s.log.Warn("site tables missing; serving them empty", "version", v.ID, "gaps", v.Result.SiteGaps)
	}
	s.log.Info("cache rebuilt", "version", v.ID, "build_ms", v.BuildTime.Milliseconds(), "load_ms", v.LoadTime.Milliseconds(),
		"assemble_ms", v.AssembleTime.Milliseconds(), "render_ms", v.RenderTime.Milliseconds(), "providers", len(v.Result.Dataset.Providers), "dataset_bytes", v.Dataset.Bytes)
	if len(unserved) > 0 {
		s.log.Warn("published products without their network pages", "version", v.ID, "pages", unserved)
	}
	return true, nil
}

func (s *Server) fail(err error) {
	s.state.Lock()
	s.state.lastError = err.Error()
	s.state.lastErrorAt = time.Now()
	s.state.failures++
	s.state.Unlock()
	s.log.Error("cache refresh failed; serving the previous version", "error", err)
}

// build reads and renders a new Version, reusing prev's bodies whose bytes
// did not change (reuse.go).
func (s *Server) build(ctx context.Context, prev *Version) (*Version, error) {
	t0 := time.Now()
	in, err := s.src.Load(ctx)
	if err != nil {
		return nil, fmt.Errorf("load: %w", err)
	}
	t1 := time.Now()
	res, err := build.Build(in)
	if err != nil {
		return nil, fmt.Errorf("build: %w", err)
	}
	t2 := time.Now()
	v, err := renderVersion(s.nextID.Add(1), res, s.opts, prev)
	if err != nil {
		return nil, err
	}
	v.LoadTime, v.AssembleTime, v.RenderTime = t1.Sub(t0), t2.Sub(t1), time.Since(t2)
	return v, nil
}

// RenderVersion pre-renders every static response of a build and, with
// opts.FilesDir, writes its dataset file there (always: no meta is read).
func RenderVersion(id uint64, res *build.Result, opts Options) (*Version, error) {
	v, err := renderVersion(id, res, opts, nil)
	if err != nil || opts.FilesDir == "" {
		return v, err
	}
	f, err := newDatasetFiles(opts.FilesDir, "", slog.Default()).write(res.Dataset)
	if err != nil {
		return nil, err
	}
	return v, setDataset(v, f)
}

func renderVersion(id uint64, res *build.Result, opts Options, prev *Version) (*Version, error) {
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
	var mu sync.Mutex
	var wg sync.WaitGroup
	errs := make(chan error, len(jobs)+2)
	for _, j := range jobs {
		wg.Add(1)
		go func(j job) {
			defer wg.Done()
			r, err := reused.render(http.StatusOK, j.value, j.cache)
			if err != nil {
				errs <- fmt.Errorf("render %s: %w", j.path, err)
				return
			}
			mu.Lock()
			v.Static[j.path] = r
			mu.Unlock()
		}(j)
	}
	wg.Wait()
	close(errs)
	if err := <-errs; err != nil {
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
	ver, err := Render(http.StatusOK, VersionInfo{Revision: Revision(v), BuiltAt: v.Result.BuiltAt.UTC()}, CacheVersion, LevelFast)
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
}

// Revision identifies the published data of a Version: the hex SHA-256 of the
// dataset file, which is also its ETag and part of its name.
func Revision(v *Version) string {
	if v.Dataset == nil {
		return ""
	}
	return v.Dataset.SHA256
}

// Run polls for changes until the context ends.
func (s *Server) Run(ctx context.Context) {
	t := time.NewTicker(s.opts.Interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			cctx, cancel := context.WithTimeout(ctx, 2*time.Minute)
			_, _ = s.Refresh(cctx, false)
			cancel()
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
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		writeError(w, http.StatusNotFound, "not found")
	})
	return mux
}

func writeError(w http.ResponseWriter, status int, msg string) {
	body, _ := Marshal(map[string]string{"error": msg})
	h := w.Header()
	h.Set("Content-Type", "application/json; charset=utf-8")
	h.Set("Cache-Control", CacheNone)
	h.Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	w.Write(body) //nolint:errcheck
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
		writeError(w, http.StatusServiceUnavailable, "cache is not built yet")
		return
	}
	body, ok := v.Static[path]
	if !ok {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	s.write(w, r, v, body)
}

func (s *Server) serveSearch(w http.ResponseWriter, r *http.Request) {
	v := s.current.Load()
	if v == nil {
		writeError(w, http.StatusServiceUnavailable, "cache is not built yet")
		return
	}
	for k := range r.URL.Query() {
		if k != "q" {
			writeError(w, http.StatusBadRequest, "unknown query parameter "+k)
			return
		}
	}
	q := r.URL.Query().Get("q")
	if len(q) > 200 {
		writeError(w, http.StatusBadRequest, "query too long")
		return
	}
	key := strings.Join(build.Tokens(q), " ")
	body, ok := v.search.Get(key)
	if !ok {
		var err error
		body, err = Render(http.StatusOK, v.Result.Search.Search(key), CacheData, LevelFast)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		v.search.Add(key, body)
	}
	s.write(w, r, v, body)
}

// MaxEvents is the framework's /v1/events limit.
const MaxEvents = 500

var categories = map[string]bool{"census": true, "pulse": true, "incident": true, "audit": true}

// ParseEventFilter validates /v1/events parameters like the framework does.
func ParseEventFilter(q map[string][]string) (store.EventFilter, error) {
	f := store.EventFilter{Limit: 100}
	get := func(k string) string {
		if v := q[k]; len(v) > 0 {
			return v[0]
		}
		return ""
	}
	for k := range q {
		switch k {
		case "product", "kind", "category", "since", "limit":
		default:
			return f, errors.New("Unknown query parameter")
		}
	}
	if l := get("limit"); l != "" {
		n, err := strconv.Atoi(l)
		if err != nil || n < 1 || n > MaxEvents {
			return f, fmt.Errorf("limit must be between 1 and %d", MaxEvents)
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
		if !categories[c] {
			return f, errors.New("category must be census, pulse, incident or audit")
		}
		f.Category = c
	}
	if since := get("since"); since != "" {
		t, err := parseSince(since)
		if err != nil {
			return f, err
		}
		f.Since = &t
	}
	return f, nil
}

func parseSince(s string) (time.Time, error) {
	// URL decoding turns the "+" of "+00:00" into a space.
	s = strings.Replace(strings.TrimSpace(s), " ", "+", 1)
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05.999999999", "2006-01-02"} {
		if t, err := time.Parse(layout, s); err == nil {
			return t, nil
		}
	}
	return time.Time{}, fmt.Errorf("since must be an ISO time")
}

func eventsKey(f store.EventFilter) string {
	since := ""
	if f.Since != nil {
		since = f.Since.UTC().Format(time.RFC3339Nano)
	}
	return f.Product + "|" + strings.Join(f.Kinds, ",") + "|" + f.Category + "|" + since + "|" + strconv.Itoa(f.Limit)
}

type eventsResponse struct {
	Events []store.EventJSON `json:"events"`
}

// eventsFromWindow answers from the build's in-memory events when they
// provably hold the whole answer; ok=false means the database must be asked.
func eventsFromWindow(res *build.Result, f store.EventFilter) (out []store.EventJSON, ok bool) {
	return res.Events.Answer(f)
}

func eventsBody(res *build.Result, f store.EventFilter) (*eventsResponse, error) {
	out, ok := eventsFromWindow(res, f)
	if !ok {
		return nil, errNeedDB
	}
	return &eventsResponse{Events: out}, nil
}

var errNeedDB = errors.New("events window does not cover this filter")

func (s *Server) serveEvents(w http.ResponseWriter, r *http.Request) {
	v := s.current.Load()
	if v == nil {
		writeError(w, http.StatusServiceUnavailable, "cache is not built yet")
		return
	}
	f, err := ParseEventFilter(r.URL.Query())
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
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
			if qerr != nil {
				writeError(w, http.StatusServiceUnavailable, "events unavailable")
				s.log.Error("events fallback query failed", "error", qerr)
				return
			}
			resp = &eventsResponse{Events: make([]store.EventJSON, len(rows))}
			for i := range rows {
				resp.Events[i] = rows[i].JSON()
			}
		} else if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		body, err = Render(http.StatusOK, resp, CacheEvents, LevelFast)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		v.events.Add(key, body)
	}
	s.write(w, r, v, body)
}

// Health is /healthz.
type Health struct {
	Status      string     `json:"status"`
	Version     uint64     `json:"version"`
	BuiltAt     *time.Time `json:"builtAt"`
	BuildMs     int64      `json:"buildMs"`
	LoadMs      int64      `json:"loadMs"`
	AssembleMs  int64      `json:"assembleMs"`
	RenderMs    int64      `json:"renderMs"`
	Fingerprint string     `json:"fingerprint"`
	GeneratedAt string     `json:"generatedAt"`
	Providers   int        `json:"providers"`
	LastCheck   *time.Time `json:"lastCheck"`
	LastError   *string    `json:"lastError"`
	LastErrorAt *time.Time `json:"lastErrorAt"`
	Builds      int        `json:"builds"`
	Failures    int        `json:"failures"`
	SearchCache int        `json:"searchCacheEntries"`
	EventsCache int        `json:"eventsCacheEntries"`
	// Unserved lists the pages a published product's network should have
	// that do not answer 200; any makes the status degraded.
	Unserved []string `json:"unserved"`
	// SiteGaps lists the site tables (or columns) the database lacks, served
	// as empty; informational, the status stays ok (`siteapi migrate` adds them).
	SiteGaps []string `json:"siteGaps"`
	// Binary is the source revision this siteapi was built from.
	Binary Binary `json:"binary"`
}

func (s *Server) serveHealth(w http.ResponseWriter, r *http.Request) {
	v := s.current.Load()
	s.state.Lock()
	h := Health{Status: "ok", Builds: s.state.builds, Failures: s.state.failures, Unserved: append([]string{}, s.state.unserved...),
		SiteGaps: append([]string{}, s.state.siteGaps...), Binary: BinaryInfo()}
	if len(h.Unserved) > 0 {
		h.Status = "degraded"
	}
	if !s.state.lastCheck.IsZero() {
		t := s.state.lastCheck
		h.LastCheck = &t
	}
	if s.state.lastError != "" {
		e, t := s.state.lastError, s.state.lastErrorAt
		h.LastError, h.LastErrorAt = &e, &t
		h.Status = "degraded"
	}
	s.state.Unlock()
	status := http.StatusOK
	if v == nil {
		h.Status = "starting"
		status = http.StatusServiceUnavailable
	} else {
		t := v.BuiltAt
		h.Version, h.BuiltAt, h.BuildMs, h.Fingerprint = v.ID, &t, v.BuildTime.Milliseconds(), v.Fingerprint
		h.LoadMs, h.AssembleMs, h.RenderMs = v.LoadTime.Milliseconds(), v.AssembleTime.Milliseconds(), v.RenderTime.Milliseconds()
		h.GeneratedAt, h.Providers = v.Result.Dataset.GeneratedAt, len(v.Result.Dataset.Providers)
		h.SearchCache, h.EventsCache = v.search.Len(), v.events.Len()
	}
	body, _ := Marshal(h)
	hd := w.Header()
	hd.Set("Content-Type", "application/json; charset=utf-8")
	hd.Set("Cache-Control", CacheNone)
	w.WriteHeader(status)
	if r.Method != http.MethodHead {
		w.Write(body) //nolint:errcheck
	}
}
