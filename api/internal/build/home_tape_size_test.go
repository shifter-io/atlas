package build

import (
	"encoding/json"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// homeWithTapeMax is the /v1/home size rule with a full tape (TAPE-VALUES-1,
// #906): every range at most 190 KB, about 10 KB under the 204,800-byte
// MaxAnswerBytes. The fixture overstates production: its 7d is 187,983
// bytes against about 165.5 KB estimated for production's 7d. Removing the
// tape rows' title, detail and value later gives the bytes back.
const homeWithTapeMax = 190 << 10

// TestEveryHomeBodyWithAFullTapeFits builds TestEveryHomeBodyFitsWithRoom's
// real-shape fixture with a full home tape (testscale.TapeEvents: the newest
// 10 rows of each tab, HOME-TAPE-1, of the real kinds and data, with
// country, region, target, severity and values) and fails when a range is
// over homeWithTapeMax. It logs the tapes' bytes and each range's total.
func TestEveryHomeBodyWithAFullTapeFits(t *testing.T) {
	t.Parallel()
	in, err := testscale.WeekInputs(testscale.Shape{Networks: 18, Countries: 1, Exits: 3, Tail: 30, Browser: true, SuccessScores: true, Gaps: true})
	if err != nil {
		t.Fatal(err)
	}
	in.TapeHeads = testscale.TapeEvents(18)
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	logTapeBytes(t, res)
	for _, r := range Ranges {
		body := res.Pages.Registered[homePath(r.ID)].(*HomeAnswer)
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		t.Logf("/v1/home/%s with its tape: %d bytes, %d tape rows", r.ID, len(raw), len(body.Tape))
		if len(body.Networks) != 18 || len(body.Tape) != 40 {
			t.Errorf("%s: %d networks, %d tape rows: not the real shape", r.ID, len(body.Networks), len(body.Tape))
		}
		for _, row := range body.Tape {
			if row.Values == nil || row.Severity == nil || row.Country == nil {
				t.Errorf("%s: tape row %s lacks its values, severity or country", r.ID, row.ID)
			}
		}
		if len(raw) > homeWithTapeMax {
			t.Errorf("/v1/home/%s is %d bytes, above %d", r.ID, len(raw), homeWithTapeMax)
		}
	}
}

// logTapeBytes logs the size of the tapes (the home's, a network answer's and
// the dataset's), with and without TAPE-VALUES-1's fields.
func logTapeBytes(t *testing.T, res *Result) {
	t.Helper()
	network := res.Pages.Network["net00/residential"]
	if network == nil {
		t.Fatal("no network answer net00/residential")
	}
	tapes := map[string][]model.TapeEvent{"home": res.Pages.Registered[homePath("7d")].(*HomeAnswer).Tape,
		"network net00": network.Tape, "dataset": res.Dataset.Tape}
	for name, tape := range tapes {
		raw, err := json.Marshal(tape)
		if err != nil {
			t.Fatal(err)
		}
		var rows []map[string]any
		if err := json.Unmarshal(raw, &rows); err != nil {
			t.Fatal(err)
		}
		for _, r := range rows {
			for _, k := range tapeContractNull {
				delete(r, k)
			}
		}
		before, _ := json.Marshal(rows)
		t.Logf("%s tape: %d rows, %d bytes (%d per row), %d without TAPE-VALUES-1's fields", name, len(tape), len(raw),
			len(raw)/max(len(tape), 1), len(before))
	}
}
