package store

import (
	"bytes"
	"context"
	"math"

	"github.com/jackc/pgx/v5"
)

// placeRowsSQL reads every country, region, city and ASN row of the build's
// target-free windows from its typed rows: the published snapshot keeps only
// the top rows of the region, city and ASN lists (longtail.go), and the site
// ranks and lists every place. Only the fields a rank or a place list needs are read.
const placeRowsSQL = `SELECT w.product_id, w.cohort, w.window_hours,
 CASE d.dimension WHEN 'geography' THEN 'city' ELSE d.dimension END,
 d.label->>'country_code', d.label->>'country', d.label->>'region_code', d.label->>'region', d.label->>'city',
 (d.label->>'asn')::bigint, d.label->>'name', d.ips, d.completed, d.successes, d.p50::double precision
 FROM public.calculated_windows w
 JOIN public.calculated_breakdown_details d ON d.result_id = w.result_id
 WHERE w.build_id = $1 AND w.target = '' AND d.dimension IN ('country', 'region', 'geography', 'asn')`

// PlaceRow is one country, region, city or ASN row of a product window, in the
// payload's breakdown names (Dimension "country", "region", "city" or "asn").
// Missing numbers are NaN.
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

// readPlaceRows reads the place lists when the snapshot is capped.
func (s *SnapshotRow) readPlaceRows(ctx context.Context, tx pgx.Tx) error {
	if !bytes.Contains(s.Payload, []byte(`"breakdown_entries"`)) {
		return nil
	}
	rows, err := tx.Query(ctx, placeRowsSQL, s.BuildID)
	if err != nil {
		return err
	}
	defer rows.Close()
	places := map[PlaceWindow][]PlaceRow{}
	for rows.Next() {
		var k PlaceWindow
		var r PlaceRow
		var asn, ips, completed, successes *int64
		var p50 *float64
		if err := rows.Scan(&k.Product, &k.Cohort, &k.Hours, &r.Dimension, &r.CountryCode, &r.Country, &r.RegionCode,
			&r.Region, &r.City, &asn, &r.Name, &ips, &completed, &successes, &p50); err != nil {
			return err
		}
		r.Asn, r.Ips, r.Completed, r.Successes, r.P50 = intOrNaN(asn), intOrNaN(ips), intOrNaN(completed), intOrNaN(successes), math.NaN()
		if p50 != nil {
			r.P50 = *p50
		}
		places[k] = append(places[k], r)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	s.Places = places
	return nil
}

func intOrNaN(v *int64) float64 {
	if v == nil {
		return math.NaN()
	}
	return float64(*v)
}
