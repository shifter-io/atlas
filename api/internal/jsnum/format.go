package jsnum

import "math"

// NA is the one string the site shows wherever it has no value (format.ts).
const NA = "N/A"

// Missing mirrors format.ts isMissing for numbers.
func Missing(x float64) bool { return math.IsNaN(x) || math.IsInf(x, 0) }

// Pct is format.ts pct.
func Pct(x float64, dp int) string {
	if Missing(x) {
		return NA
	}
	return ToFixed(x*100, dp) + "%"
}

// Count is format.ts count.
func Count(x float64) string {
	if Missing(x) {
		return NA
	}
	return Locale(x)
}

// Ms is format.ts ms.
func Ms(x float64) string {
	if Missing(x) {
		return NA
	}
	if x >= 1000 {
		return ToFixed(x/1000, 2) + " s"
	}
	return ToFixed(Round(x), 0) + " ms"
}

// Duration is format.ts duration (minutes in).
func Duration(minutes float64) string {
	if Missing(minutes) {
		return NA
	}
	if minutes < 60 {
		return ToFixed(Round(minutes), 0) + " min"
	}
	h := math.Floor(minutes / 60)
	m := Round(math.Mod(minutes, 60))
	if m != 0 {
		return ToFixed(h, 0) + "h " + ToFixed(m, 0) + "m"
	}
	return ToFixed(h, 0) + "h"
}
