package server

import (
	"bytes"
	"context"
	"encoding/json"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// renamedSource is the fresh source with its network under the framework id
// "iproyal": the snapshot names it "Iproyal" (the framework's id, title
// cased) and its catalog row "IPRoyal", with its own tagline, one event and
// one changelog row.
type renamedSource struct{ *freshSource }

const catalogTagline = "Get access to over 64M+ ethically-sourced residential proxies in more than 195 locations."

func newRenamedSource(t *testing.T) renamedSource {
	t.Helper()
	f := newFreshSource(t, store.ProviderRow{Slug: "iproyal", Name: "IPRoyal", Tagline: sptr(catalogTagline),
		Kinds: []string{"residential"}, PrimaryKind: sptr("residential"), MeasuredProduct: sptr("residential_rotating"),
		Website: sptr("https://iproyal.com"), FrameworkProvider: sptr("iproyal"), Listed: true})
	f.payload = bytes.ReplaceAll(bytes.ReplaceAll(f.payload, []byte(`"shifter`), []byte(`"iproyal`)), []byte(`"Shifter"`), []byte(`"Iproyal"`))
	return renamedSource{f}
}

func (r renamedSource) events() []store.Event {
	return []store.Event{event(1, time.Now().UTC().Add(-time.Hour), "pulse_down", "iproyal")}
}

func (r renamedSource) Load(ctx context.Context) (*store.Inputs, error) {
	in, err := r.freshSource.Load(ctx)
	if err != nil {
		return nil, err
	}
	in.FrameworkProviders = []string{"iproyal"}
	in.FrameworkProducts[0].Provider = "iproyal"
	in.Events = r.events()
	in.Changelog = []store.ChangeRow{{ID: "ch1", Kind: "score", ProviderSlug: sptr("iproyal"), Headline: "Score moved",
		Unit: "pts", Severity: "info", OccurredAt: time.Now().UTC().Add(-2 * time.Hour)}}
	return in, nil
}

func (r renamedSource) LoadEvents(context.Context) (*store.Inputs, error) {
	return &store.Inputs{Events: r.events(), EventsComplete: true, NotableComplete: true}, nil
}

// A name field holding the framework id or its title-cased form.
var frameworkName = regexp.MustCompile(`"(name|providerName|networkName|title)":"(iproyal|Iproyal)"`)

// Every answer that names a network takes the catalog's name, never the
// framework's id or a title-cased form of it.
func TestEveryAnswerNamesTheNetworkAsTheCatalogDoes(t *testing.T) {
	t.Parallel()
	s, _ := freshServer(t, newRenamedSource(t))
	for _, path := range []string{"/v1/rankings", "/v1/networks", "/v1/networks/all", "/v1/networks/iproyal", "/v1/home",
		"/v1/targets", "/v1/overlap", "/v1/search?q=iproyal", "/v1/search-index", "/v1/events", "/v1/events/stats", "/v1/dataset"} {
		rec := get(s.Handler(), path)
		body := rec.Body.String()
		if rec.Code != 200 {
			t.Errorf("%s: %d %.300s", path, rec.Code, body)
			continue
		}
		// The all-networks answer has no name (null); with one network it names no other.
		if path != "/v1/networks/all" && !strings.Contains(body, `"IPRoyal"`) {
			t.Errorf("%s does not name IPRoyal: %.300s", path, body)
		}
		if m := frameworkName.FindString(body); m != "" || strings.Contains(body, "Iproyal") {
			t.Errorf("%s names the network by its framework id: %q", path, m)
		}
	}
}

// The catalog's tagline and the generated schedule are English copy, not
// served (API-PROSE-REMOVAL-1, #962): no home row, ranking row or network
// answer carries them, though the catalog row has a tagline.
func TestTheCatalogTaglineIsNotServed(t *testing.T) {
	t.Parallel()
	s, _ := freshServer(t, newRenamedSource(t))
	var home struct {
		Networks []map[string]any `json:"networks"`
	}
	rec := get(s.Handler(), "/v1/home")
	if err := json.Unmarshal(rec.Body.Bytes(), &home); err != nil || len(home.Networks) != 1 {
		t.Fatalf("home: %v %.300s", err, rec.Body.String())
	}
	for _, k := range []string{"tagline", "schedule", "latencyNote"} {
		if _, ok := home.Networks[0][k]; ok || home.Networks[0]["name"] != "IPRoyal" {
			t.Errorf("home network %v serves %s", home.Networks[0]["name"], k)
		}
	}
	for _, path := range []string{"/v1/home", "/v1/rankings", "/v1/networks/iproyal"} {
		if body := get(s.Handler(), path).Body.String(); strings.Contains(body, catalogTagline) || strings.Contains(body, "measured on a") {
			t.Errorf("%s serves the tagline or the schedule: %.400s", path, body)
		}
	}
}
