package server

import (
	"encoding/json"
	"fmt"
	"net/netip"
	"regexp"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// The privacy scan (SEC-EVENTS-SAFE-1): no answer, list page, error answer
// or dataset file carries an address outside the documentation ranges, an
// e-mail address, a secret or a local path. It walks the bodies of the
// English scan (englishBodies, over the scan build and testscale.Week) and,
// in TestNoAnswerNamesTheIPDataVendor, the committed fixtures' builds.

// privacyShapes are what the scan looks for, besides addresses (addressHits).
var privacyShapes = []struct {
	what string
	re   *regexp.Regexp
}{
	{"e-mail address", regexp.MustCompile(`[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}`)},
	{"connection string with credentials", regexp.MustCompile(`[a-z][a-z0-9+.-]*://[^/\s:@"]+:[^/\s@"]*@`)},
	{"password", regexp.MustCompile(`(?i)passw(or)?d`)},
	{"private key", regexp.MustCompile(`BEGIN [A-Z ]*PRIVATE KEY`)},
	{"token", regexp.MustCompile(`\b(dop_v[1]_[a-f0-9]{16,}|xox[abprs]-[A-Za-z0-9-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|(AKIA|ASIA)[A-Z0-9]{16})\b`)},
	{"local path", regexp.MustCompile(`(/home/|/root/|/User[s]/|/var/|/opt/|\.secrets\b)`)},
}

var (
	ipv4Like = regexp.MustCompile(`\b(\d{1,3}\.){3}\d{1,3}\b`)
	ipv6Like = regexp.MustCompile(`(?i)[0-9a-f]*(:[0-9a-f]*){2,8}`)
	// docRanges are the documentation addresses an answer may hold.
	docRanges = []netip.Prefix{netip.MustParsePrefix("192.0.2.0/24"), netip.MustParsePrefix("198.51.100.0/24"),
		netip.MustParsePrefix("203.0.113.0/24"), netip.MustParsePrefix("2001:db8::/32")}
)

// privacyExceptions are the hits the scan allows, each with why: a body's
// name prefix and what it holds there. None yet.
var privacyExceptions = []struct{ body, what, why string }{}

// privacyHits is what of privacyShapes and addresses body holds, each with
// a masked example (a hit's own text never reaches a log).
func privacyHits(body []byte) map[string]string {
	hits := map[string]string{}
	for _, s := range privacyShapes {
		if m := s.re.Find(body); m != nil {
			hits[s.what] = mask(string(m))
		}
	}
	for what, m := range addressHits(body) {
		hits[what] = mask(m)
	}
	return hits
}

// addressHits is the first IPv4 and IPv6 address of body outside docRanges.
func addressHits(body []byte) map[string]string {
	hits := map[string]string{}
	for what, re := range map[string]*regexp.Regexp{"IPv4 address": ipv4Like, "IPv6 address": ipv6Like} {
		for _, m := range re.FindAll(body, -1) {
			a, err := netip.ParseAddr(string(m))
			if err != nil || a.IsUnspecified() || documentation(a) {
				continue
			}
			hits[what] = string(m)
			break
		}
	}
	return hits
}

func documentation(a netip.Addr) bool {
	for _, p := range docRanges {
		if p.Contains(a) {
			return true
		}
	}
	return false
}

// mask is s's first 3 bytes and its length.
func mask(s string) string { return fmt.Sprintf("%.3s… (%d bytes)", s, len(s)) }

// privacyFindings is every hit of bodies privacyExceptions does not allow,
// sorted, and the exceptions it used.
func privacyFindings(bodies map[string][]byte) (found []string, used map[int]bool) {
	used = map[int]bool{}
	for name, b := range bodies {
		for what, example := range privacyHits(b) {
			allowed := false
			for i, e := range privacyExceptions {
				if strings.HasPrefix(name, e.body) && e.what == what {
					allowed, used[i] = true, true
				}
			}
			if !allowed {
				found = append(found, fmt.Sprintf("%s: %s %s", name, what, example))
			}
		}
	}
	sort.Strings(found)
	return found, used
}

func checkPrivacy(t *testing.T, bodies map[string][]byte) {
	t.Helper()
	found, _ := privacyFindings(bodies)
	if len(found) > 0 {
		t.Errorf("%d bodies carry private data:\n%s", len(found), strings.Join(found, "\n"))
	}
}

// Every body of the scan build and of the week carries no address, e-mail
// address, secret or path; every exception is used.
func TestNoPrivateDataInAnswers(t *testing.T) {
	t.Parallel()
	week, err := testscale.NetworkInputs(testscale.Week.NetworkShape)
	if err != nil {
		t.Fatal(err)
	}
	bodies, srv := serveScan(t, week, testscale.Week.Networks)
	all := map[string][]byte{}
	for name, b := range routeBodies(t, bodies, srv) {
		all["week "+name] = b
	}
	for name, b := range englishBodies(t) {
		all[name] = b
	}
	if len(all) < 100 || len(all["dataset file"]) == 0 || len(all["week dataset file"]) == 0 {
		t.Fatalf("%d bodies scanned", len(all))
	}
	checkPrivacy(t, all)
	_, used := privacyFindings(all)
	for i, e := range privacyExceptions {
		if !used[i] {
			t.Errorf("exception %s %s (%s) allows nothing: remove it", e.body, e.what, e.why)
		}
	}
}

// The scan fails on an address in an event's data, and finds each shape.
func TestPrivacyScanFindsSeededData(t *testing.T) {
	t.Parallel()
	public := strings.Join([]string{"45", "33", "12", "7"}, ".") // no literal public address in the source
	tape := testscale.TapeEvents(3)
	for i := range tape {
		if tape[i].Kind == "new_hosting_asn" {
			tape[i].Data = json.RawMessage(`{"asn": 1, "name": "exit ` + public + `"}`)
			break
		}
	}
	r := get(newTestServer(t, &fakeSource{rev: 1, events: tape, complete: true}).Handler(), "/v1/events?limit=500")
	if found, _ := privacyFindings(map[string][]byte{"/v1/events": r.Body.Bytes()}); len(found) != 1 || !strings.Contains(found[0], "IPv4") {
		t.Fatalf("seeded address: %v", found)
	}
	at := "@"
	for body, want := range map[string]string{
		"ops" + at + "mail.example.net": "e-mail address", "postgres://u:s" + at + "db/x": "connection string with credentials",
		"db_password": "password", "-----BEGIN RSA PRIV" + "ATE KEY-----": "private key",
		"dop_v" + "1_" + strings.Repeat("ab", 32): "token", "AKIA" + strings.Repeat("A1", 8): "token",
		"/ho" + "me/ops/x": "local path", "/U" + "sers/ops/x": "local path", "x/.sec" + "rets/y": "local path",
		strings.Join([]string{"2a01", "4f8", "", "1"}, ":"): "IPv6 address", "2001:db8::1 192.0.2.4 " + time.Now().UTC().Format(time.RFC3339): "",
	} {
		hits := privacyHits([]byte(body))
		if _, ok := hits[want]; (want == "" && len(hits) > 0) || (want != "" && !ok) {
			t.Errorf("%.12s…: %v, want %q", body, hits, want)
		}
	}
}
