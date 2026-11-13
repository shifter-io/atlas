package server

import (
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// Data answers (pages, search, coverage) are cached, by browsers and by the
// CDN in front of the API, until the next scheduled data update: the CDN's copy
// never outlives it, and a browser's copy may outlive it by at most max-age
// unless the CDN sends Age. Most of their content changes only with a new
// snapshot, but some of it is built from test events and the site catalog,
// which change at any time: the dataset's tape and every network answer's tape
// and changelog. A catalog edit triggers a rebuild of its own; a new test event
// refreshes only /v1/events (reuse.go), so the tapes keep the events of the
// last build. Those parts lag up to the next update. /v1/version and the /v1/dataset redirect stay short
// (CacheVersion), so open pages still notice every change within seconds.
const (
	// DefaultDataPeriod is the data update schedule: a new snapshot is
	// published once per period, with its cutoff on the period's UTC grid.
	DefaultDataPeriod = time.Hour
	// DefaultCacheFloor is the TTL when the next update's time is unknown (no
	// published snapshot) or already due (the update is late): short, so new
	// data is picked up soon after it appears.
	DefaultCacheFloor = 30 * time.Second
)

// ValidDataPeriod reports whether period can be a data update schedule: it
// must be positive and divide 24 hours, so its grid (time.Truncate's, anchored
// at a UTC midnight) is the same every UTC day.
func ValidDataPeriod(period time.Duration) error {
	if period <= 0 || (24*time.Hour)%period != 0 {
		return fmt.Errorf("data period %v must be positive and divide 24h", period)
	}
	return nil
}

// nextData is when the update after a snapshot cut off at asOf is due: the
// end of asOf's period on the UTC grid. Zero when asOf is missing or
// unparseable or period is not positive.
func nextData(asOf model.Str, period time.Duration) time.Time {
	if asOf == nil || period <= 0 {
		return time.Time{}
	}
	t, ok := snap.ParseTime(*asOf)
	if !ok {
		return time.Time{}
	}
	return t.UTC().Truncate(period).Add(period)
}

// dataCache sets Cache-Control (max-age and s-maxage) and Expires on a data
// answer: cached until next, rounded down to whole seconds so it never
// outlives next, or for floor when next is unknown or not in the future.
func dataCache(h http.Header, now, next time.Time, floor time.Duration) {
	ttl := next.Sub(now)
	if next.IsZero() || ttl <= 0 {
		ttl = floor
	}
	secs := max(int64(ttl/time.Second), 1)
	n := strconv.FormatInt(secs, 10)
	h.Set("Cache-Control", "public, max-age="+n+", s-maxage="+n)
	h.Set("Expires", now.Add(time.Duration(secs)*time.Second).UTC().Format(http.TimeFormat))
}

// setDataCache applies dataCache with the server's clock and floor, or, with
// a CDN configured, cdnCache (cdn.go).
func (s *Server) setDataCache(h http.Header, v *Version) {
	if s.opts.CDNTTL > 0 {
		cdnCache(h, s.opts.Now(), s.opts.CDNTTL, s.opts.CDNTag)
		return
	}
	var next time.Time
	if v != nil {
		next = v.NextData
	}
	dataCache(h, s.opts.Now(), next, s.opts.CacheFloor)
}
