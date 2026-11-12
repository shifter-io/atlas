package server

import (
	"os"
	"regexp"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/config"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// vendorName is the IP data vendor's name in any spelling and case.
var vendorName = regexp.MustCompile(`(?i)db[-_]?ip`)

// NO-VENDOR-NAME-1: no served answer and no dataset file names the IP data
// vendor. Every answer of each committed fixture's build is rendered (the
// stored answers, the dataset file and every registered page's samples, on
// request) and scanned.
func TestNoAnswerNamesTheIPDataVendor(t *testing.T) {
	t.Parallel()
	pages := append([]string{}, goldenPages...)
	for _, a := range build.Answers() {
		pages = append(pages, a.Samples...)
	}
	for _, file := range []string{"snapshot.json", "snapshot-every-request.json", "head-v4-browser-scores.json"} {
		t.Run(file, func(t *testing.T) {
			t.Parallel()
			raw, err := os.ReadFile(config.FindUp("fixtures/measured/" + file))
			if err != nil {
				t.Fatal(err)
			}
			db := testdb.New(t)
			var id int64
			if file == "head-v4-browser-scores.json" {
				db.Exec(t, testscale.BrowserTargetsSQL())
				id = db.Publish(t, raw)
			} else {
				id = db.PublishV4(t, raw)
			}
			answers := renderedAnswers(t, db, id, pages, func(*build.Result) {})
			if len(answers) < 20 || len(answers["dataset"]) == 0 {
				t.Fatalf("%d answers rendered, dataset of %d bytes", len(answers), len(answers["dataset"]))
			}
			for name, b := range answers {
				if m := vendorName.Find(b); m != nil {
					t.Errorf("%s names the IP data vendor: %q", name, m)
				}
			}
		})
	}
}
