package server

import (
	"net/http"
	"time"
)

// unavailable answers 503: nothing serves yet, or, while the current build
// is older than payload v4, its reason.
func (s *Server) unavailable(w http.ResponseWriter) {
	s.state.Lock()
	msg := s.state.reason
	s.state.Unlock()
	if msg == "" {
		msg = "cache is not built yet"
	}
	writeError(w, http.StatusServiceUnavailable, msg)
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
	// Reason is why nothing serves: BeforeV4Reason, else null.
	Reason      *string `json:"reason"`
	Builds      int     `json:"builds"`
	Failures    int     `json:"failures"`
	SearchCache int     `json:"searchCacheEntries"`
	EventsCache int     `json:"eventsCacheEntries"`
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
