package server

import (
	"bytes"
	"encoding/json"
	"fmt"
	"slices"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// tapePages are the live tape's reads of withTapeBurst, in the events golden.
var tapePages = []string{"/v1/events?after=0&limit=100", "/v1/events?after=100&limit=100", "/v1/events?after=200&limit=100",
	"/v1/events?network=shifter&limit=5", "/v1/events?product=shifter&limit=5", "/v1/events?q=shifter+de&limit=5",
	"/v1/events?country=DE&target=generic_http&proxyKind=residential&limit=5", "/v1/events?network=nope"}

// withTapeBurst gives the build, in place of its events (none), a detect
// burst of 300 Shifter events sharing one at: pulses in US and DE, and a
// pulse down in DE as event 150, with their counts as a read in their
// bucket (12:00 to 12:30) gives them: the trigger's row and the 24 newest
// signals. The dataset keeps its tape.
func withTapeBurst(res *build.Result) {
	at := time.Date(2026, 11, 3, 12, 0, 0, 0, time.UTC)
	stats := store.EventStats{End: at.Add(30 * time.Minute), Buckets: []store.EventBucket{{Start: at, Provider: sptr("shifter"),
		Category: "pulse", Events: 300, Bad: 1, LastAt: at}}}
	rows := []store.Event{}
	for id := int64(300); id >= 1; id-- {
		e := store.Event{ID: id, At: at, Kind: "pulse_finished", Provider: sptr("shifter"), Product: sptr("residential"), Country: sptr("US"),
			Target: sptr("generic_http"), Severity: "info", Data: json.RawMessage(`{"requests":100,"success_rate":0.97,"p50":420}`)}
		if id%2 == 0 {
			e.Country = sptr("DE")
		}
		if id == 150 {
			e.Kind, e.Signal, e.Severity, e.Data = "pulse_down", -1, "major", json.RawMessage(`{"requests":100,"errors":100}`)
		}
		rows = append(rows, e)
		if len(stats.Recent) < store.RecentSignals {
			stats.Recent = append(stats.Recent, store.RecentSignal{Provider: "shifter", At: at, ID: id, Signal: e.Signal})
		}
	}
	*res = *build.WithEvents(res, &store.Inputs{Fingerprint: res.Fingerprint, Events: rows, EventsComplete: true, EventStats: stats})
}

// The every-request build's live tape (TAPE-ROWS-1): rows name the network
// by its site slug, network= answers as product= does, an unknown network
// answers 400, q matches words of the text, and the 300-event burst read
// with limit=100 and after loses none.
func TestTapeRowsOfTheEveryRequestBuild(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	payload, err := testscale.EveryRequest()
	if err != nil {
		t.Fatal(err)
	}
	answers := renderedAnswers(t, db, db.PublishV4(t, payload), tapePages, withTapeBurst)
	read := func(page string) tapeBody {
		status, body, _ := bytes.Cut(answers["GET "+page], []byte(" "))
		var b tapeBody
		if string(status) != "200" || json.Unmarshal(body, &b) != nil {
			t.Fatalf("%s: %s %s", page, status, body)
		}
		return b
	}
	ids := []int64{}
	for i, page := range tapePages[:3] {
		b := read(page)
		ids = append(ids, b.ids()...)
		if *b.Cursor != int64(100*(i+1)) || b.More != (i < 2) {
			t.Fatalf("%s: cursor %d more %v", page, *b.Cursor, b.More)
		}
	}
	for i, id := range ids {
		if id != int64(i+1) {
			t.Fatalf("the burst read %d rows, row %d is %d", len(ids), i, id)
		}
	}
	if len(ids) != 300 {
		t.Fatalf("the burst read %d rows", len(ids))
	}
	network, product := read(tapePages[3]), read(tapePages[4])
	if !slices.Equal(network.ids(), product.ids()) || len(network.ids()) != 5 || *network.Events[0].ProviderSlug != "shifter" {
		t.Fatalf("network %v, product %v", network.ids(), product.ids())
	}
	// The newest pulses in DE and the pulse down (the prioritized pick), newest first.
	if q := read(tapePages[5]); !slices.Equal(q.ids(), []int64{300, 298, 296, 294, 150}) || q.Events[4].Title != "Pulse down in DE" {
		t.Fatalf("q: %v", q.ids())
	}
	if f := read(tapePages[6]); !slices.Equal(f.ids(), []int64{300, 298, 296, 294, 150}) {
		t.Fatalf("country, target and proxy kind: %v", f.ids())
	}
	if got := string(answers["GET "+tapePages[7]]); got != fmt.Sprintf("400 %s", `{"error":"unknown network nope"}`) {
		t.Fatalf("unknown network: %s", got)
	}
}
