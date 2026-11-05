package build

import (
	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
)

// addCoverageEvidence gives every measured provider of a build before
// payload v4 the rows of its network page's coverage evidence table: per
// proxy type and range, every city x ASN row of the type's headline group in
// the published order (the store reads that list whole from the build's typed
// rows, store/longtail.go), each row's IPs mapped like any breakdown row's
// (estimates.go). A v4 build has none: its rows are the dataset's
// lists.*.city_asn (DatasetLists) and its Coverage answers.
func addCoverageEvidence(entries []*entry, row *store.SnapshotRow) {
	if row != nil && row.Networks != nil {
		return
	}
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
				byRange[r.ID] = rows
			}
			e.P.CoverageEvidence[kind] = byRange
		}
	}
}
