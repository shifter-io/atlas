package server

import (
	"bytes"
	"iter"
	"math/rand/v2"
	"regexp"
	"slices"
	"strings"
	"testing"
)

// A scanPattern is a regexp of the privacy scan with the bytes its matches
// are made of and what a run of those bytes needs to hold a match. The
// scan matches the regexp on each run that may hold one, with the byte
// before and after it (so \b sees the run's neighbours), which finds the
// matches a walk of the whole body finds, in the same order, without the
// regexp machine stepping through every byte of a large body: under -race
// that walk took minutes on the week's answers (NIGHTLY-API-1).
//
// A pattern with (?i) or a negated class may match bytes from 0x80 up
// (a case fold such as U+017F for s, an invalid byte), so its bytes include
// all of them and a run never ends inside a character.
type scanPattern struct {
	re  *regexp.Regexp
	in  [256]bool
	may func(run []byte) bool
}

func newPattern(expr string, in func(c byte) bool, may func(run []byte) bool) scanPattern {
	p := scanPattern{re: regexp.MustCompile(expr), may: may}
	for c := range 256 {
		p.in[c] = in(byte(c))
	}
	return p
}

func alnum(c byte) bool { return 'a' <= c|0x20 && c|0x20 <= 'z' || '0' <= c && c <= '9' }

func hexDigit(c byte) bool { return 'a' <= c|0x20 && c|0x20 <= 'f' || '0' <= c && c <= '9' }

// has is a run holding every one of parts.
func has(parts ...string) func([]byte) bool {
	return func(run []byte) bool {
		for _, s := range parts {
			if !bytes.Contains(run, []byte(s)) {
				return false
			}
		}
		return true
	}
}

// longer is a run of at least n bytes, the shortest match.
func longer(n int) func([]byte) bool { return func(run []byte) bool { return len(run) >= n } }

// runs is every longest run of p's bytes in body that may hold a match,
// with the byte before and after it.
func (p *scanPattern) runs(body []byte) iter.Seq[[]byte] {
	return func(yield func([]byte) bool) {
		for i := 0; i < len(body); i++ {
			if !p.in[body[i]] {
				continue
			}
			j := i + 1
			for j < len(body) && p.in[body[j]] {
				j++
			}
			if p.may(body[i:j]) && !yield(body[max(i-1, 0):min(j+1, len(body))]) {
				return
			}
			i = j
		}
	}
}

// find is p.re.Find(body).
func (p *scanPattern) find(body []byte) []byte {
	for run := range p.runs(body) {
		if m := p.re.Find(run); m != nil {
			return m
		}
	}
	return nil
}

// findAll is p.re.FindAll(body, -1), one match at a time.
func (p *scanPattern) findAll(body []byte) iter.Seq[[]byte] {
	return func(yield func([]byte) bool) {
		for run := range p.runs(body) {
			for _, m := range p.re.FindAll(run, -1) {
				if !yield(m) {
					return
				}
			}
		}
	}
}

// The scan's patterns find, on runs, what their regexps find on the whole
// body, in the same order: on random bodies of the bytes and pieces around
// a match (\b neighbours, case folds, invalid bytes, partial matches).
func TestScanPatternsFindWhatTheirRegexpsFind(t *testing.T) {
	t.Parallel()
	pieces := []string{"a", "Z", "0", "7", "f", "_", "-", ".", ":", "@", "/", " ", "\"", "\n", "%", "+", "é", "ſ", "\xff", "\xc3",
		"192.0.2.4", "10.1.2.3", "1.2.3", "2001:db8::1", "fe80::", "2a01:4f8::1", "12:34:56", "::", "user@mail.example.net",
		"pg://u:s@", "://", "passw", "PaSSwOrD", "pasſwd", "BEGIN ", "RSA ", "PRIVATE KEY", "dop_v1_", "0123456789abcdef",
		"xoxb-", "ghp_", "github_pat_", "AKIA", "ASIA", "A1B2C3D4E5F6G7H8", "/home/", "/root/", "/Users/", "/var/", "/opt/", ".secrets"}
	patterns := []*scanPattern{&ipv4Like, &ipv6Like}
	for i := range privacyShapes {
		patterns = append(patterns, &privacyShapes[i].p)
	}
	r := rand.New(rand.NewPCG(1371, 1))
	found := make([]int, len(patterns))
	for range 5000 {
		var b strings.Builder
		for range 1 + r.IntN(24) {
			b.WriteString(pieces[r.IntN(len(pieces))])
		}
		body := []byte(b.String())
		for i, p := range patterns {
			want := p.re.FindAll(body, -1)
			got := slices.Collect(p.findAll(body))
			if len(got) != len(want) || (len(want) > 0 && !bytes.Equal(p.find(body), want[0])) {
				t.Fatalf("%s on %q: runs find %q, the whole body %q", p.re, body, got, want)
			}
			for k := range want {
				if &got[k][0] != &want[k][0] || len(got[k]) != len(want[k]) {
					t.Fatalf("%s on %q: runs find %q, the whole body %q", p.re, body, got, want)
				}
			}
			found[i] += len(want)
		}
	}
	for i, p := range patterns {
		if found[i] < 10 {
			t.Errorf("%s matched %d times: the bodies do not test it", p.re, found[i])
		}
	}
}
