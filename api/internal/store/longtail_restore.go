package store

// rebuild is raw with each span's value replaced by its non-nil repl, nil
// when none is.
func rebuild(raw []byte, spans []field, repl [][]byte) []byte {
	var out []byte
	at := 0
	for i, f := range spans {
		if repl[i] == nil {
			continue
		}
		out = append(append(out, raw[at:f.Start]...), repl[i]...)
		at = f.End
	}
	if out == nil {
		return nil
	}
	return append(out, raw[at:]...)
}

// breakdownNames are the breakdowns in jsonb key order.
var breakdownNames = []string{"asn", "city", "region", "country", "city_asn", "country_asn"}
