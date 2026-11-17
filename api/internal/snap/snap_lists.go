package snap

import "github.com/Zeus-Development/atlas/api/internal/jsnum"

type Census struct {
	Country    string  `json:"country"`
	Region     *string `json:"region"`
	FinishedAt *string `json:"finished_at"`
	Requests   Num     `json:"requests"`
	Successes  Num     `json:"successes"`
	UniqueIps  Num     `json:"unique_ips"`
}

func (v *Census) UnmarshalJSON(d []byte) error {
	type plain Census
	return jsnum.DecodeNA(d, (*plain)(v))
}

// Outage is one span a product was down in one country (snapshot v2
// `outages`, the newest 50 overlapping the 30d window). EndedAt is nil while
// it is still open; DurationS then runs to the snapshot's as_of.
type Outage struct {
	Country   string  `json:"country"`
	Region    *string `json:"region"`
	Source    string  `json:"source"`
	StartedAt string  `json:"started_at"`
	EndedAt   *string `json:"ended_at"`
	DurationS Num     `json:"duration_s"`
}

func (v *Outage) UnmarshalJSON(d []byte) error {
	type plain Outage
	return jsnum.DecodeNA(d, (*plain)(v))
}

type OverlapRow struct {
	Cohort    string `json:"cohort"`
	Window    string `json:"window"`
	ProductA  Num    `json:"product_a"`
	ProductB  Num    `json:"product_b"`
	SharedIps Num    `json:"shared_ips"`
	IpsA      Num    `json:"ips_a"`
	IpsB      Num    `json:"ips_b"`
}

func (v *OverlapRow) UnmarshalJSON(d []byte) error {
	type plain OverlapRow
	return jsnum.DecodeNA(d, (*plain)(v))
}

// FleetRow is one `fleet` entry: a cohort's product groups together in one
// window or, with cohort "*" (AllCohorts), every product group of the build,
// each exit IP counted once however many networks or cohorts saw it. Only the
// "*" entries carry a series (5-minute buckets for 24h, days otherwise) and IP
// slots.
type FleetRow struct {
	Cohort    string      `json:"cohort"`
	Window    string      `json:"window"`
	Products  Num         `json:"products"`
	Providers Num         `json:"providers"`
	Metrics   Metrics     `json:"metrics"`
	UniqueIps Num         `json:"unique_ips"`
	Series    []SeriesRow `json:"series"`
	// IPSlots ("*" only) is, for each UTC clock hour of 24h, the distinct IPs
	// of the trailing 24 hours to its end (FLEET-POOL-SERIES-3), and for each
	// UTC day of 7d and 30d the day's, oldest first, the last slot the one
	// holding the as-of time (it ends there); none in builds before
	// FLEET-POOL-SERIES-1 (#1302).
	IPSlots []IPSlot `json:"ip_slots"`
}

func (v *FleetRow) UnmarshalJSON(d []byte) error {
	type plain FleetRow
	return jsnum.DecodeNA(d, (*plain)(v))
}

// IPSlot is one of FleetRow.IPSlots: its start and its distinct IPs, each IP
// once however many networks saw it (NaN: no IP in the slot).
type IPSlot struct {
	At        string `json:"at"`
	UniqueIps Num    `json:"unique_ips"`
}

func (v *IPSlot) UnmarshalJSON(d []byte) error {
	type plain IPSlot
	return jsnum.DecodeNA(d, (*plain)(v))
}

// AllCohorts is the fleet cohort of every product group together.
const AllCohorts = "*"

// FleetAll is the window's every-group fleet entry, nil when the snapshot
// does not publish one (builds before it did).
func (v *Snapshot) FleetAll(window string) *FleetRow {
	for i := range v.Fleet {
		if f := &v.Fleet[i]; f.Cohort == AllCohorts && f.Window == window {
			return f
		}
	}
	return nil
}
