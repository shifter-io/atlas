package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"

	"github.com/Zeus-Development/atlas/api/internal/build"
)

// One build behind every range (API-SNAPSHOT-CONSISTENCY-1, #1267). A visitor
// compares home, rankings, targets, overlap and the networks list across 24h,
// 7d and 30d; each must come from the same build. A full build renders them
// all from one build.Result, so each carries its GeneratedAt: renderVersion
// checks that on the stored bodies before a Version can serve, and refuses
// the build when one is missing or from another build, so the serving one
// keeps answering every range. What a CDN keeps in front of the API is
// checked from outside, through the public site (scripts/server-alerts/
// checks.sh, "public snapshot").

// ConsistencyRequests are the requests whose answers carry one generatedAt.
var ConsistencyRequests = consistencyRequests()

func consistencyRequests() []string {
	out := []string{"/v1/networks"}
	for _, r := range build.RangeIDs {
		for _, a := range []string{"home", "rankings", "targets", "overlap"} {
			out = append(out, "/v1/"+a+"?range="+r)
		}
	}
	return out
}

// storedPath is the stored path (Version.Static) that request reads.
func storedPath(request string) (string, error) {
	u, err := url.Parse(request)
	if err != nil {
		return "", err
	}
	switch u.Path {
	case "/v1/networks":
		return u.Path, nil
	case "/v1/rankings":
		r, err := build.QueryRange(u.Query())
		return "/v1/rankings/" + r, err
	}
	for _, a := range build.Answers() {
		if a.Route == "GET "+u.Path && a.Cut == nil {
			return a.Key(&http.Request{Method: http.MethodGet, URL: u})
		}
	}
	return "", fmt.Errorf("%s is no stored answer", request)
}

// snapshotConsistent refuses (Refused) v when an answer of
// ConsistencyRequests is missing or carries another generatedAt than v's
// build.
func snapshotConsistent(v *Version) error {
	want := v.Result.Dataset.GeneratedAt
	var bad []string
	for _, req := range ConsistencyRequests {
		path, err := storedPath(req)
		if err != nil {
			return err
		}
		got, err := generatedAt(v.Static[path])
		switch {
		case err != nil:
			bad = append(bad, req+" "+err.Error())
		case got != want:
			bad = append(bad, req+" is from "+got)
		}
	}
	if len(bad) > 0 {
		return &Refused{fmt.Errorf("answers not from the build of %s: %s", want, strings.Join(bad, "; "))}
	}
	return nil
}

// generatedAt is a stored body's top-level generatedAt.
func generatedAt(r *Rendered) (string, error) {
	if r == nil {
		return "", errors.New("was not built")
	}
	var b struct {
		GeneratedAt *string `json:"generatedAt"`
	}
	if err := json.Unmarshal(r.Identity, &b); err != nil || b.GeneratedAt == nil {
		return "", errors.New("has no generatedAt")
	}
	return *b.GeneratedAt, nil
}
