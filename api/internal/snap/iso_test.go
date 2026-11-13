package snap

import (
	"encoding/json"
	"testing"
	"time"
)

// Every form the framework and the database write becomes JSISO's, the same
// instant to the millisecond (CONSISTENCY-1); other text stays as it is.
func TestISOText(t *testing.T) {
	t.Parallel()
	for in, want := range map[string]string{
		"2026-11-11T14:09:42.371728+00:00":    "2026-11-11T14:09:42.371Z",
		"2026-11-09T04:02:32.000Z":            "2026-11-09T04:02:32.000Z",
		"2026-11-09T04:02:32Z":                "2026-11-09T04:02:32.000Z",
		"2026-11-09T04:02:32.123456789Z":      "2026-11-09T04:02:32.123Z",
		"2026-11-09T06:02:32+02:00":           "2026-11-09T04:02:32.000Z",
		"2026-11-09T04:02:32.5":               "2026-11-09T04:02:32.500Z",
		"2026-11-09 04:02:32.250000+00:00":    "2026-11-09T04:02:32.250Z",
		"2026-11-09":                          "2026-11-09",
		"net-2026-11-09T04:02:32+00:00":       "net-2026-11-09T04:02:32+00:00",
		"2026-11-09T04:02:32+00:00 and later": "2026-11-09T04:02:32+00:00 and later",
	} {
		if got := ISOText(in); got != want {
			t.Errorf("ISOText(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestISOInJSON(t *testing.T) {
	t.Parallel()
	in := `{"since":"2026-11-03T03:40:00+00:00","n":3,"day":"2026-11-03","x":{"at":["2026-11-03T03:00:00.123456+00:00"]},"s":"a \"2026\""}`
	want := `{"since":"2026-11-03T03:40:00.000Z","n":3,"day":"2026-11-03","x":{"at":["2026-11-03T03:00:00.123Z"]},"s":"a \"2026\""}`
	if got := string(ISOInJSON([]byte(in))); got != want {
		t.Errorf("ISOInJSON = %s, want %s", got, want)
	}
	if plain := []byte(`{"requests":100}`); &ISOInJSON(plain)[0] != &plain[0] {
		t.Error("JSON without a time is copied")
	}
	if ISOInJSON(nil) != nil {
		t.Error("nil is not nil")
	}
}

func TestTimeEncodesAsJSISO(t *testing.T) {
	t.Parallel()
	at := time.Date(2026, 11, 13, 10, 0, 0, 123456789, time.FixedZone("x", 3600))
	b, err := json.Marshal(struct{ At *Time }{At(at)})
	if err != nil || string(b) != `{"At":"2026-11-13T09:00:00.123Z"}` {
		t.Fatalf("%s %v", b, err)
	}
	var back struct{ At *Time }
	if err := json.Unmarshal(b, &back); err != nil || !time.Time(*back.At).Equal(at.Truncate(time.Millisecond)) {
		t.Fatalf("%v %v", back.At, err)
	}
	if json.Unmarshal([]byte(`{"At":"yesterday"}`), &back) == nil {
		t.Error("text that is no time decoded")
	}
}
