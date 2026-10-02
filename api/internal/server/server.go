// Package server serves the precomputed responses and keeps them fresh.
//
// A Version is an immutable set of rendered bodies built from one database
// read. Requests only ever look up bytes in the current Version (an atomic
// pointer), so they never touch the database; the refresh loop builds the
// next Version in the background and swaps it in. A failed build leaves the
// previous Version serving and is reported by /healthz.
package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/ice-zeus/milf-website/api/internal/build"
	"github.com/ice-zeus/milf-website/api/internal/store"
	"github.com/jackc/pgx/v5/pgxpool"
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
}

// Source loads inputs; the database in production, fixtures in tests.
type Source interface {
	Fingerprint(ctx context.Context) (store.Fingerprint, error)
	Load(ctx context.Context) (*store.Inputs, error)
	Events(ctx context.Context, f store.EventFilter) ([]store.Event, error)
}

// DBSource reads through a read-only pool.
type DBSource struct{ Pool *pgxpool.Pool }

func (d DBSource) Fingerprint(ctx context.Context) (store.Fingerprint, error) {
	return store.ReadFingerprint(ctx, d.Pool)
}

func (d DBSource) Load(ctx context.Context) (*store.Inputs, error) { return store.Load(ctx, d.Pool) }

func (d DBSource) Events(ctx context.Context, f store.EventFilter) ([]store.Event, error) {
	return store.QueryEvents(ctx, d.Pool, f)
}

// Options configure a Server.
type Options struct {
	Interval  time.Duration
	Content   *build.Content
	SearchLRU int
	EventsLRU int
	Logger    *slog.Logger
}

// Server holds the current Version and refresh state.
type Server struct {
	src       Source
	opts      Options
	current   atomic.Pointer[Version]
	refreshMu sync.Mutex
	nextID    atomic.Uint64
	state     struct {
		sync.Mutex
		lastError   string
		lastErrorAt time.Time
		lastCheck   time.Time
		builds      int
		failures    int
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
	return &Server{src: src, opts: opts, log: opts.Logger}
}

// Current returns the serving Version (nil before the first build).
func (s *Server) Current() *Version { return s.current.Load() }

// Refresh rebuilds when the fingerprint changed (or force). Concurrent calls
// are single-flight: one build at a time, and a caller that waited re-checks
// the fingerprint instead of building the same thing twice.
func (s *Server) Refresh(ctx context.Context, force bool) (bool, error) {
	s.refreshMu.Lock()
	defer s.refreshMu.Unlock()
	fp, err := s.src.Fingerprint(ctx)
	s.state.Lock()
	s.state.lastCheck = time.Now()
	s.state.Unlock()
	if err != nil {
		s.fail(fmt.Errorf("fingerprint: %w", err))
		return false, err
	}
	cur := s.current.Load()
	if !force && cur != nil && cur.Fingerprint == fp.Key() {
		return false, nil
	}
	start := time.Now()
	v, err := s.build(ctx)
	if err != nil {
		s.fail(err)
		return false, err
	}
	v.BuildTime = time.Since(start)
	s.current.Store(v)
	s.state.Lock()
	s.state.builds++
	s.state.lastError = ""
	s.state.Unlock()
	s.log.Info("cache rebuilt", "version", v.ID, "build_ms", v.BuildTime.Milliseconds(), "load_ms", v.LoadTime.Milliseconds(),
		"assemble_ms", v.AssembleTime.Milliseconds(), "render_ms", v.RenderTime.Milliseconds(), "providers", len(v.Result.Dataset.Providers), "dataset_bytes", len(v.Static["/v1/dataset"].Identity))
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

func (s *Server) build(ctx context.Context) (*Version, error) {
	t0 := time.Now()
	in, err := s.src.Load(ctx)
	if err != nil {
		return nil, fmt.Errorf("load: %w", err)
	}
	t1 := time.Now()
	res, err := build.Build(in, build.Options{Content: s.opts.Content})
	if err != nil {
		return nil, fmt.Errorf("build: %w", err)
	}
	t2 := time.Now()
	v, err := RenderVersion(s.nextID.Add(1), res, s.opts)
	if err != nil {
		return nil, err
	}
	v.LoadTime, v.AssembleTime, v.RenderTime = t1.Sub(t0), t2.Sub(t1), time.Since(t2)
	return v, nil
}

// RenderVersion pre-renders every static response of a build.
func RenderVersion(id uint64, res *build.Result, opts Options) (*Version, error) {
	v := &Version{ID: id, Fingerprint: res.Fingerprint.Key(), BuiltAt: res.BuiltAt, Result: res, Static: map[string]*Rendered{},
		search: NewLRU(max(opts.SearchLRU, 1)), events: NewLRU(max(opts.EventsLRU, 1))}
	type job struct {
		path  string
		value any
		cache string
	}
	jobs := []job{
		{"/v1/dataset", res.Dataset, CacheStatic},
		{"/v1/overview", res.Overview, CacheStatic},
		{"/v1/providers", map[string]any{"generatedAt": res.Dataset.GeneratedAt, "providers": res.Providers}, CacheStatic},
		{"/v1/search-index", res.Search, CacheStatic},
		{"/v1/coverage", res.Coverage, CacheStatic},
	}
	for slug, d := range res.Details {
		jobs = append(jobs, job{"/v1/providers/" + slug, d, CacheStatic})
	}
	var mu sync.Mutex
	var wg sync.WaitGroup
	errs := make(chan error, len(jobs)+2)
	for _, j := range jobs {
		wg.Add(1)
		go func(j job) {
			defer wg.Done()
			r, err := Render(http.StatusOK, j.value, j.cache, LevelBest)
			if err != nil {
				errs <- fmt.Errorf("render %s: %w", j.path, err)
				return
			}
			mu.Lock()
			v.Static[j.path] = r
			mu.Unlock()
		}(j)
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		var r *Rendered
		if res.SnapshotPayload != nil {
			r = RenderBytes(http.StatusOK, compactJSON(res.SnapshotPayload), CacheStatic, LevelBest)
		} else {
			var err error
			r, err = Render(http.StatusNotFound, map[string]any{"error": "no published snapshot", "available": false}, CacheStatic, LevelFast)
			if err != nil {
				errs <- err
				return
			}
		}
		mu.Lock()
		v.Static["/v1/snapshot"] = r
		mu.Unlock()
	}()
	wg.Wait()
	close(errs)
	if err := <-errs; err != nil {
		return nil, err
	}
	// What a browser polls to learn that the data changed. The revision is the
	// dataset body's own hash (its ETag without quotes), so it changes exactly
	// when the published data does, and a site built from a `siteapi dump` file
	// can compute it from the bytes. No extra database work: it is rendered once
	// per Version, like everything else.
	ver, err := Render(http.StatusOK, VersionInfo{Revision: Revision(v), BuiltAt: res.BuiltAt.UTC()}, CacheVersion, LevelFast)
	if err != nil {
		return nil, err
	}
	v.Static["/v1/version"] = ver
	// The default events query (what the live tape polls first) is rendered up front.
	// (When the in-memory window cannot prove the answer, the first request asks the database.)
	if def, err := eventsBody(res, store.EventFilter{Limit: 100}); err == nil {
		r, err := Render(http.StatusOK, def, CacheEvents, LevelBest)
		if err != nil {
			return nil, err
		}
		v.events.Add(eventsKey(store.EventFilter{Limit: 100}), r)
	}
	return v, nil
}

// VersionInfo is /v1/version.
type VersionInfo struct {
	Revision string    `json:"revision"`
	BuiltAt  time.Time `json:"builtAt"`
}

// Revision identifies the published data of a Version: the hex SHA-256 of the
// /v1/dataset body, which is also that body's ETag.
func Revision(v *Version) string {
	d := v.Static["/v1/dataset"]
	if d == nil {
		return ""
	}
	return strings.Trim(d.ETag, `"`)
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
	for _, p := range []string{"/v1/dataset", "/v1/overview", "/v1/providers", "/v1/search-index", "/v1/coverage", "/v1/snapshot", "/v1/version"} {
		mux.HandleFunc("GET "+p, static(p))
	}
	mux.HandleFunc("GET /v1/providers/{slug}", func(w http.ResponseWriter, r *http.Request) {
		s.serveStatic(w, r, "/v1/providers/"+r.PathValue("slug"))
	})
	mux.HandleFunc("GET /v1/search", s.serveSearch)
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
		body, err = Render(http.StatusOK, v.Result.Search.Search(key), CacheStatic, LevelFast)
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

// eventsFromWindow answers from the in-memory newest-events window when it
// provably holds the whole answer; ok=false means the database must be asked.
func eventsFromWindow(res *build.Result, f store.EventFilter) (out []store.EventJSON, ok bool) {
	out = []store.EventJSON{}
	for i := range res.RawEvents {
		if f.Match(&res.RawEvents[i]) {
			out = append(out, res.Events[i])
			if len(out) == f.Limit {
				return out, true
			}
		}
	}
	if res.EventsComplete {
		return out, true
	}
	if f.Since != nil && len(res.RawEvents) > 0 && !res.RawEvents[len(res.RawEvents)-1].At.After(*f.Since) {
		return out, true
	}
	return out, false
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
}

func (s *Server) serveHealth(w http.ResponseWriter, r *http.Request) {
	v := s.current.Load()
	s.state.Lock()
	h := Health{Status: "ok", Builds: s.state.builds, Failures: s.state.failures}
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

// compactJSON strips insignificant whitespace (Postgres jsonb text has some).
func compactJSON(b []byte) []byte {
	var buf bytes.Buffer
	if err := json.Compact(&buf, b); err != nil {
		return b
	}
	return buf.Bytes()
}
