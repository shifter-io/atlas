package server

// What a refresh redoes. A new test event changes only the newest event id in
// the fingerprint, and only the events answers read the events directly, so
// such a change reads the events alone and makes a Version that shares every
// other body (refreshEvents): no snapshot decode, no build, no compression.
// The network answers' tapes are built from events too; they keep the events
// of the last full build until the next one (a new snapshot or a catalog
// edit), as cached copies of those answers already lag up to the next data
// update (cache.go). The dataset file's tape keeps those of the build that
// last wrote it (datasetfile.go). A full build re-renders everything
// but keeps the previous Version's body wherever the new bytes are the same
// (bodies.render), so only bodies that changed are compressed again.

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"sort"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// versionPath is /v1/version, rendered per Version at LevelFast.
const versionPath = "/v1/version"

type changeKind int

const (
	changeNone   changeKind = iota
	changeData              // full build
	changeEvents            // refreshEvents
)

// change classifies a polled fingerprint against the serving Version: nothing
// served yet, force or a changed data part needs a full build; a change of the
// newest event id alone, or events above the cursor bound (which a later read
// raises), needs only the events answers.
func change(cur *Version, fp store.Fingerprint, force bool) changeKind {
	switch {
	case force || cur == nil || cur.Result.Fingerprint.DataKey() != fp.DataKey():
		return changeData
	case cur.Result.Fingerprint.EventsKey() != fp.EventsKey(), cur.Result.Events.Pending():
		return changeEvents
	}
	return changeNone
}

// refreshEvents serves the current events without a full build. It reads only
// the events (Source.LoadEvents) and swaps in a copy of cur with the new
// events window, a new events LRU (its default query pre-rendered) and a new
// ID; the rendered answers, the dataset file and the search and page LRUs are
// cur's own. A failure keeps cur serving and is reported like a failed build.
func (s *Server) refreshEvents(ctx context.Context, cur *Version) (bool, error) {
	start := time.Now()
	in, err := s.src.LoadEvents(ctx)
	if err != nil {
		err = fmt.Errorf("load events: %w", err)
		s.fail(err)
		return false, err
	}
	v := *cur
	v.ID = s.nextID.Add(1)
	v.Result = build.WithEvents(cur.Result, in)
	v.Fingerprint = v.Result.Fingerprint.Key()
	v.events = NewLRU(max(s.opts.EventsLRU, 1))
	if err := addDefaultEvents(&v); err != nil {
		s.fail(err)
		return false, err
	}
	s.current.Store(&v)
	s.state.Lock()
	s.state.lastError = ""
	s.state.Unlock()
	s.log.Info("events refreshed", "version", v.ID, "refresh_ms", time.Since(start).Milliseconds(), "events", len(in.Events),
		"notable", len(in.Notable))
	return true, nil
}

// addDefaultEvents pre-renders the default events query (what the live tape
// polls first) into v's events LRU. When the in-memory window cannot prove the
// answer, nothing is added and the first request asks the database.
func addDefaultEvents(v *Version) error {
	f := store.EventFilter{Limit: 100}
	def, err := eventsBody(v.Result, f)
	if err != nil {
		return nil
	}
	r, err := Render(http.StatusOK, def, CacheEvents, LevelBest)
	if err != nil {
		return err
	}
	v.events.Add(eventsKey(f), r)
	return nil
}

// bodyKey identifies a rendered body by its identity bytes' SHA-256 (the
// quoted hex ETag), its Cache-Control and its status.
type bodyKey struct {
	etag, cache string
	status      int
}

// bodies is a previous Version's precomputed bodies: its static answers
// except /v1/version. All of them were rendered at LevelBest, like every job
// of renderVersion.
type bodies map[bodyKey]*Rendered

// reusable indexes prev's bodies (none for a nil prev).
func reusable(prev *Version) bodies {
	out := bodies{}
	if prev == nil {
		return out
	}
	add := func(r *Rendered) {
		if r != nil {
			out[bodyKey{r.ETag, r.CacheControl, r.Status}] = r
		}
	}
	for path, r := range prev.Static {
		if path != versionPath {
			add(r)
		}
	}
	return out
}

// shareIdentical points every path of static whose body has the same bytes,
// cache policy and status as another's at one body (the first path's in
// order), as reusable does across versions: a rankings answer of the one kind
// measured is the answer of every kind, and the empty kinds' are alike.
func shareIdentical(static map[string]*Rendered) {
	paths := make([]string, 0, len(static))
	for p := range static {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	first := bodies{}
	for _, p := range paths {
		r := static[p]
		k := bodyKey{r.ETag, r.CacheControl, r.Status}
		if f, ok := first[k]; ok {
			static[p] = f
		} else {
			first[k] = r
		}
	}
}

// render is Render at LevelBest, except that when the encoded bytes, cache
// policy and status are those of an indexed body, that body (its pointer and
// compressed variants) is returned instead of compressing the bytes again.
func (b bodies) render(status int, value any, cache string) (*Rendered, error) {
	body, err := Marshal(value)
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(body)
	if old, ok := b[bodyKey{`"` + hex.EncodeToString(sum[:]) + `"`, cache, status}]; ok && bytes.Equal(old.Identity, body) {
		return old, nil
	}
	return RenderBytes(status, body, cache, LevelBest), nil
}
