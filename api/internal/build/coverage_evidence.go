package build

import (
	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
)

// addCoverageEvidence gives every measured provider the rows of its network
// page's coverage evidence table: per proxy type and range, every city x ASN
// row of the type's headline group in the published order (the store reads
// that list whole from the build's typed rows, store/longtail.go; a v4 build
// reads its network list in place), each row's IPs mapped like any breakdown
// row's (estimates.go).
func addCoverageEvidence(entries []*entry, row *store.SnapshotRow) {
	for _, e := range entries {
		if len(e.Snap) == 0 {
			continue
		}
		e.P.CoverageEvidence = map[string]map[string][]model.CoverageRow{}
		for kind, p := range e.Snap {
			byRange := map[string][]model.CoverageRow{}
			for _, r := range Ranges {
				w := p.Window(r.ID)
				rows := make([]model.CoverageRow, 0, len(w.Breakdowns["city_asn"]))
				for _, b := range w.Breakdowns["city_asn"] {
					rows = append(rows, model.CoverageRow{CountryCode: b.CountryCode, Country: b.Country, Region: b.Region, City: b.City,
						Asn: b.Asn, AsnName: b.Name, UsageType: b.UsageType, Ips: scaledIps(w, b.Ips), LatencyP50: b.P50})
				}
				if l := netListOf(row, p, w); l != nil {
					for i := range l.Len("city_asn") {
						rows = append(rows, coverageRow(l.Row("city_asn", i), breakdownScale(w)))
					}
				}
				byRange[r.ID] = rows
			}
			e.P.CoverageEvidence[kind] = byRange
		}
	}
}
