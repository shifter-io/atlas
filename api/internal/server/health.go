package server

import (
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// Health is /healthz.
type Health struct {
	Status     string     `json:"status"`
	Version    uint64     `json:"version"`
	BuiltAt    *time.Time `json:"builtAt"`
	BuildMs    int64      `json:"buildMs"`
	LoadMs     int64      `json:"loadMs"`
	AssembleMs int64      `json:"assembleMs"`
	RenderMs   int64      `json:"renderMs"`
	// Fingerprint is the serving version's source fingerprint
	// (store.Fingerprint), null before the first build.
	Fingerprint json.RawMessage `json:"fingerprint"`
	GeneratedAt string          `json:"generatedAt"`
	Providers   int             `json:"providers"`
	LastCheck   *time.Time      `json:"lastCheck"`
	LastError   *string         `json:"lastError"`
	LastErrorAt *time.Time      `json:"lastErrorAt"`
	// Reason is why nothing serves, an unavailable_reason code:
	// BeforeV4Reason, else null.
	Reason      *string `json:"reason"`
	Builds      int     `json:"builds"`
	Failures    int     `json:"failures"`
	SearchCache int     `json:"searchCacheEntries"`
	EventsCache int     `json:"eventsCacheEntries"`
	// Unserved lists the pages a published product's network should have
	// that do not answer 200, the first MaxUnserved, and UnservedMore counts
	// the rest; any makes the status degraded.
	Unserved     []Unserved `json:"unserved"`
	UnservedMore int        `json:"unservedMore"`
	// SiteGaps lists the site tables (or columns) the database lacks, served
	// as empty; informational, the status stays ok (`siteapi migrate` adds them).
	SiteGaps []string `json:"siteGaps"`
	// ColdLists is what the serving build's warm step left cold.
	ColdLists ColdLists `json:"coldLists"`
	// Binary is the source revision this siteapi was built from.
	Binary Binary `json:"binary"`
	// FailedFingerprint is the build id of the last failed build (null when
	// none failed or it had none), with its error and when; RetryAt is when
	// its fingerprint may be built again, null when refused (retry.go). A
	// failed build makes the status degraded until a build succeeds.
	FailedFingerprint *int64     `json:"failedFingerprint"`
	FailedError       *string    `json:"failedError"`
	FailedAt          *time.Time `json:"failedAt"`
	RetryAt           *time.Time `json:"retryAt"`
}

func (s *Server) serveHealth(w http.ResponseWriter, r *http.Request) {
	v := s.current.Load()
	s.state.Lock()
	h := Health{Status: "ok", Builds: s.state.builds, Failures: s.state.failures, Unserved: append([]Unserved{}, s.state.unserved...),
		UnservedMore: s.state.unservedMore, SiteGaps: append([]string{}, s.state.siteGaps...), Binary: BinaryInfo()}
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
	s.failedHealth(&h)
	reason := s.state.reason
	s.state.Unlock()
	status := http.StatusOK
	switch {
	case v == nil && reason != "":
		h.Status, h.Reason = "unavailable", &reason
		status = http.StatusServiceUnavailable
	case v == nil:
		h.Status = "starting"
		status = http.StatusServiceUnavailable
	default:
		t := v.BuiltAt
		h.Version, h.BuiltAt, h.BuildMs = v.ID, &t, v.BuildTime.Milliseconds()
		if json.Valid([]byte(v.Fingerprint)) {
			h.Fingerprint = json.RawMessage(v.Fingerprint)
		}
		h.LoadMs, h.AssembleMs, h.RenderMs = v.LoadTime.Milliseconds(), v.AssembleTime.Milliseconds(), v.RenderTime.Milliseconds()
		h.GeneratedAt, h.Providers = v.Result.Dataset.GeneratedAt, len(v.Result.Dataset.Providers)
		h.SearchCache, h.EventsCache, h.ColdLists = v.search.Len(), v.events.Len(), v.coldLists
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

// ColdLists is /healthz coldLists: how many network list dimensions the warm
// step left out of the row cache (--row-cache too small), their rows, and the
// largest of them (null when none). Their first searches read from the
// database; informational, the status stays ok.
type ColdLists struct {
	Count   int             `json:"count"`
	Rows    int             `json:"rows"`
	Largest *store.ColdList `json:"largest"`
}

// warmed logs what a build put in the row cache before it serves
// (store.NetRowSet.Warm), warns about the lists the cache's limit left out,
// each with its rows, and returns them for /healthz.
func (s *Server) warmed(id uint64, w store.Warmed) ColdLists {
	if w.Dimensions+w.ColdDimensions == 0 {
		return ColdLists{}
	}
	s.log.Info("row cache warmed", "version", id, "dimensions", w.Dimensions, "rows", w.Rows, "bytes", w.Bytes, "limit_bytes", w.Limit)
	if w.ColdDimensions == 0 {
		return ColdLists{}
	}
	lists := make([]string, len(w.Cold))
	for i, c := range w.Cold {
		lists[i] = fmt.Sprintf("%d/%s:%d", c.List, c.Dimension, c.Rows)
	}
	s.log.Warn("row cache too small: lists left cold (siteapi serve -row-cache)", "version", id, "dimensions", w.ColdDimensions,
		"rows", w.ColdRows, "lists", lists)
	return ColdLists{Count: w.ColdDimensions, Rows: w.ColdRows, Largest: &w.Cold[0]}
}
