package server

import (
	"maps"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
)

// DATASET-DOWNLOAD-1: every dataset file answer (GET and HEAD, each coding,
// and a 304) says Content-Disposition: attachment, so a cross-origin link
// downloads the file instead of opening it in a tab; every other header is
// what it was before.
func TestTheDatasetFileAnswersAsADownload(t *testing.T) {
	t.Parallel()
	s := newTestServer(t, &fakeSource{rev: 1})
	h := s.Handler()
	v := s.Current()
	f := v.Dataset
	common := http.Header{
		"Vary":                        {"Accept-Encoding"},
		"Cache-Control":               {CacheFile},
		"X-Content-Type-Options":      {"nosniff"},
		"Access-Control-Allow-Origin": {"*"},
		"X-Site-Version":              {strconv.FormatUint(v.ID, 10)},
		"Content-Disposition":         {`attachment; filename="shifter-proxy-index-dataset.json"`},
	}
	for _, e := range []Encoding{Identity, Gzip, Brotli} {
		_, coding, length, etag := f.variant(e)
		accept := map[Encoding]string{Identity: "", Gzip: "gzip", Brotli: "br"}[e]
		for _, method := range []string{http.MethodGet, http.MethodHead} {
			want := maps.Clone(common)
			want["Etag"] = []string{etag}
			want["Content-Type"] = []string{"application/json; charset=utf-8"}
			want["Content-Length"] = []string{strconv.FormatInt(length, 10)}
			if coding != "" {
				want["Content-Encoding"] = []string{coding}
			}
			r := request(h, method, v.DatasetPath, "Accept-Encoding", accept)
			if r.Code != http.StatusOK || !equalHeaders(r.Header(), want) {
				t.Errorf("%s %q: %d %v; want %v", method, accept, r.Code, r.Header(), want)
			}
		}
		want := maps.Clone(common)
		want["Etag"] = []string{etag}
		r := request(h, http.MethodGet, v.DatasetPath, "Accept-Encoding", accept, "If-None-Match", etag)
		if r.Code != http.StatusNotModified || !equalHeaders(r.Header(), want) {
			t.Errorf("304 %q: %d %v; want %v", accept, r.Code, r.Header(), want)
		}
	}
	// The redirect to the file is unchanged: no Content-Disposition.
	if r := getOnce(h, "/v1/dataset"); r.Code != http.StatusFound || r.Header().Get("Content-Disposition") != "" {
		t.Errorf("/v1/dataset: %d %v", r.Code, r.Header())
	}
}

// request sends method to path with the headers (name, value pairs; an empty
// value is not sent), not following a redirect.
func request(h http.Handler, method, path string, headers ...string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, nil)
	for i := 0; i+1 < len(headers); i += 2 {
		if headers[i+1] != "" {
			req.Header.Set(headers[i], headers[i+1])
		}
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

// equalHeaders reports whether got has exactly want's headers.
func equalHeaders(got, want http.Header) bool {
	return maps.EqualFunc(got, want, func(a, b []string) bool {
		return len(a) == len(b) && (len(a) == 0 || a[0] == b[0])
	})
}
