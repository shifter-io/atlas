package store

import "math"

// The site ranks and lists every place: the published snapshot keeps only the
// top rows of the region, city and ASN lists, so SnapshotRow.Places holds
// each target-free window's typed list (lists.go), whose country, region,
// city and ASN rows the build reads whole.

// PlaceRow is a list row as a rank or a place list reads it, in the payload's
// breakdown names (Dimension "country", "region", "city" or "asn"). Missing
// numbers are NaN.
type PlaceRow struct {
	Dimension                                      string
	CountryCode, Country, RegionCode, Region, City *string
	Name                                           *string // the ASN's name
	Asn, Ips, Completed, Successes, P50            float64
}

// PlaceWindow names a product window in SnapshotRow.Places.
type PlaceWindow struct {
	Product int64
	Cohort  string
	Hours   int64
}

// PlaceRow is the row in a dimension's terms.
func (r *ListRow) PlaceRow(dimension string) PlaceRow {
	out := PlaceRow{Dimension: dimension, Asn: math.NaN(), Ips: intOrNaN(r.Ips), Completed: intOrNaN(r.Completed),
		Successes: intOrNaN(r.Successes), P50: intOrNaN(int64(r.P50))}
	if p := r.Place; p != nil {
		out.CountryCode, out.Country, out.RegionCode, out.Region, out.City = p.CountryCode, p.Country, p.RegionCode, p.Region, p.City
	}
	if n := r.Network; n != nil {
		out.Name = n.Name
		if n.Asn != nil {
			out.Asn = float64(*n.Asn)
		}
	}
	return out
}

// intOrNaN is a counter as a number, NaN when NULL (-1).
func intOrNaN(v int64) float64 {
	if v < 0 {
		return math.NaN()
	}
	return float64(v)
}
