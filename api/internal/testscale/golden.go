package testscale

import "regexp"

// latencyMeanAdded is what LAT-MEAN-API adds to an answer built from a
// snapshot without the latency mean: the null performance.latencyMean
// member and its N/A path (also under productMeasurements.<key>).
var latencyMeanAdded = regexp.MustCompile(`"latencyMean":null,|"(productMeasurements\.[^".]+\.)?performance\.latencyMean",`)

// WithoutLatencyMean is b without what LAT-MEAN-API added, every other byte
// as written, so tests pinned to answers from before it still compare them.
func WithoutLatencyMean(b []byte) []byte {
	return latencyMeanAdded.ReplaceAll(b, nil)
}
