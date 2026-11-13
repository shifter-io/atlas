package server

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// The English guard's pending list (ENGLISH-GUARD-1, #976): the English
// still served, by area, with the task that removes it.

// pendingLine is one line of the pending list: its area (file), the task
// that removes the path's English, and whether only a served API has it
// (third column "live": the scan build has no such value).
type pendingLine struct {
	area, task string
	live       bool
}

// readPending reads testdata/english-pending/*.txt: a path, a tab, the task
// and optionally a tab and "live"; # starts a comment.
func readPending(t *testing.T) map[string]pendingLine {
	t.Helper()
	files, err := filepath.Glob("testdata/english-pending/*.txt")
	if err != nil || len(files) == 0 {
		t.Fatalf("no pending list: %v", err)
	}
	out := map[string]pendingLine{}
	for _, f := range files {
		area := strings.TrimSuffix(filepath.Base(f), ".txt")
		file, err := os.Open(f)
		if err != nil {
			t.Fatal(err)
		}
		sc := bufio.NewScanner(file)
		for sc.Scan() {
			line := strings.TrimSpace(sc.Text())
			if line == "" || strings.HasPrefix(line, "#") {
				continue
			}
			cols := strings.Split(line, "\t")
			if len(cols) < 2 || len(cols) > 3 || (len(cols) == 3 && cols[2] != "live") {
				t.Errorf("%s: want \"<path>\\t<task>[\\tlive]\", got %q", f, line)
				continue
			}
			if prev, dup := out[cols[0]]; dup {
				t.Errorf("%s: %s is listed in %s.txt too", f, cols[0], prev.area)
			}
			out[cols[0]] = pendingLine{area, cols[1], len(cols) == 3}
		}
		file.Close()
	}
	return out
}

// checkPending fails t on every English path pending does not list and, when
// strict (the scan build), on every listed path that is clean; a served API
// (not strict) only logs those, as its data has paths the build has not.
func checkPending(t *testing.T, hits map[string]englishHit, pending map[string]pendingLine, strict bool) {
	t.Helper()
	unlisted, clean := pendingGaps(hits, pending, strict)
	if len(unlisted) > 0 {
		t.Errorf("%d paths serve English (a value, not a word for display: or list the path in testdata/english-pending/<area>.txt with its task):\n%s",
			len(unlisted), strings.Join(unlisted, "\n"))
	}
	switch {
	case len(clean) > 0 && strict:
		t.Errorf("%d listed paths serve no English: delete their lines:\n%s", len(clean), strings.Join(clean, "\n"))
	case len(clean) > 0:
		t.Logf("%d listed paths serve no English here:\n%s", len(clean), strings.Join(clean, "\n"))
	}
}

// pendingGaps is every English path pending does not list (with its kind and
// example), and every listed path that is clean (when strict, but for the
// live lines), sorted.
func pendingGaps(hits map[string]englishHit, pending map[string]pendingLine, strict bool) (unlisted, clean []string) {
	for p, h := range hits {
		if _, ok := pending[p]; !ok {
			unlisted = append(unlisted, fmt.Sprintf("%s\t%s: %q", p, h.kind, h.example))
		}
	}
	for p, l := range pending {
		if _, ok := hits[p]; !ok && (!strict || !l.live) {
			clean = append(clean, fmt.Sprintf("%s\t%s (%s.txt)", p, l.task, l.area))
		}
	}
	sort.Strings(unlisted)
	sort.Strings(clean)
	return unlisted, clean
}

func TestPendingGaps(t *testing.T) {
	t.Parallel()
	hits := map[string]englishHit{"/v1/a.x": {"sentence", "A b c d"}, "/v1/a.y": {"display word", "Yes"}}
	pending := map[string]pendingLine{"/v1/a.x": {"times", "R1", false}, "/v1/a.z": {"times", "R1", false},
		"/v1/a.w": {"profile", "R4", true}}
	unlisted, clean := pendingGaps(hits, pending, true)
	if strings.Join(unlisted, ",") != `/v1/a.y	display word: "Yes"` || strings.Join(clean, ",") != "/v1/a.z\tR1 (times.txt)" {
		t.Errorf("strict: unlisted %q, clean %q", unlisted, clean)
	}
	if _, clean = pendingGaps(hits, pending, false); len(clean) != 2 {
		t.Errorf("live: clean %q, want the live line too", clean)
	}
}
