package build

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"testing"
)

// networkBodyMax is the /v1/networks/{slug} size rule (NETWORK-PAGE-SIZE-1,
// #938, api/FIELDS.md, the network page, "Size"): every network answer at most
// 180 KB at the real shape, about 20 KB under MaxAnswerBytes, so a page
// nears the cap here and not in production (as /v1/home did, #905).
const networkBodyMax = 180 << 10

// TestEveryNetworkBodyFitsWithRoom builds networkScaleInputs (18 networks
// at production's largest counts) and fails when a network answer (the
// all-networks one too) is over networkBodyMax or a network's is not of the
// real shape. It logs each answer's size and the largest one's bytes per
// top-level, profile and ranges.30d key.
func TestEveryNetworkBodyFitsWithRoom(t *testing.T) {
	t.Parallel()
	res, err := Build(networkScaleInputs(t))
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Pages.Network) != networkScale.Networks+1 {
		t.Fatalf("%d network answers, want %d and the all-networks one", len(res.Pages.Network), networkScale.Networks)
	}
	keys := make([]string, 0, len(res.Pages.Network))
	for key := range res.Pages.Network {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	largest, largestBytes := "", 0
	for _, key := range keys {
		ans := res.Pages.Network[key]
		raw, err := json.Marshal(ans)
		if err != nil {
			t.Fatal(err)
		}
		t.Logf("/v1/networks/%s: %d bytes", key, len(raw))
		if ans.Networks == 0 { // the all-networks answer has no profile, tape or changelog
			checkNetworkShape(t, key, ans)
		}
		if len(raw) > networkBodyMax {
			t.Errorf("/v1/networks/%s is %d bytes, above %d", key, len(raw), networkBodyMax)
		}
		if len(raw) > largestBytes {
			largest, largestBytes = key, len(raw)
		}
	}
	logKeyBytes(t, "/v1/networks/"+largest, res.Pages.Network[largest], largestBytes)
	logKeyBytes(t, "/v1/networks/"+largest+" profile", res.Pages.Network[largest].Profile, largestBytes)
	logKeyBytes(t, "/v1/networks/"+largest+" ranges.30d", res.Pages.Network[largest].Ranges["30d"], largestBytes)
}

// checkNetworkShape fails when a network answer lacks networkScale's
// counts, so the size rule never passes on a smaller page.
func checkNetworkShape(t *testing.T, key string, ans *NetworkAnswer) {
	t.Helper()
	got := map[string]int{"requestedCountries": len(ans.RequestedCountries), "requestedCountryStatus": len(ans.RequestedCountryStatus),
		"days": len(ans.Days), "tape": len(ans.Tape), "changelog": len(ans.Changelog)}
	want := map[string]int{"requestedCountries": networkScale.Requested, "requestedCountryStatus": networkScale.Requested,
		"days": 30, "tape": networkScale.Tape, "changelog": RecordCap}
	for _, d := range ans.Days {
		if d.SuccessScore != nil && len(d.SuccessScore.Targets) >= 12 {
			got["scored days"]++
		}
	}
	want["scored days"] = 30
	if p := ans.Profile; p != nil {
		got["plans"], got["incidents"], got["targets"] = len(p.Pricing.Plans), len(p.Incidents), len(p.Targets)
		got["overlaps"] = len(p.Overlap.Overlaps)
	}
	want["plans"], want["incidents"], want["targets"], want["overlaps"] = networkScale.Plans, networkScale.Incidents, 12, networkScale.Networks-1
	for _, r := range Ranges {
		if rt := ans.Ranges[r.ID]; rt != nil {
			got[r.ID+" countries"], got[r.ID+" targets"] = len(rt.Countries), len(rt.Targets)
		}
		want[r.ID+" countries"], want[r.ID+" targets"] = 50, 12
	}
	var short []string
	for k, n := range want {
		if got[k] < n {
			short = append(short, k)
		}
	}
	if len(short) > 0 {
		sort.Strings(short)
		t.Errorf("/v1/networks/%s is not the real shape: %s short (got %v, want %v)", key, strings.Join(short, ", "), got, want)
	}
}

// logKeyBytes logs the bytes of each top-level key of v's JSON object, the
// largest first, with its share of total.
func logKeyBytes(t *testing.T, name string, v any, total int) {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(raw, &obj); err != nil {
		t.Fatal(err)
	}
	keys := make([]string, 0, len(obj))
	for k := range obj {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool { return len(obj[keys[i]]) > len(obj[keys[j]]) })
	var b strings.Builder
	for _, k := range keys {
		fmt.Fprintf(&b, "\n  %s: %d bytes, %.1f%%", k, len(obj[k]), 100*float64(len(obj[k]))/float64(total))
	}
	t.Logf("%s, %d bytes by key:%s", name, len(raw), b.String())
}
