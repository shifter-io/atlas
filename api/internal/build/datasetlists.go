package build

import (
	"bufio"
	"bytes"
	"encoding/json"
	"strconv"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// datasetDimensions are the dimensions of a network window in the dataset's
// lists, in the order they are written.
var datasetDimensions = [...]string{"country", "region", "city", "asn", "city_asn"}

// DatasetLists is the dataset file's `lists` (contract K11, DATASET-SIZE-2):
// lists[slug][kind][range][dimension] is that network kind's
// list answer without a query as {"total", "pages", "rows"}: its row count,
// its page count and its first page (at most PageSize rows, as compact arrays
// in the order FIELDS.md gives each dimension), byte for byte the values the
// first page of /v1/networks/{slug}/places (or /coverage) serves. Every
// further page is read from the API. It holds the build's lists (placeIndex,
// coverageList), whose first pages the build keeps, so writing it reads
// nothing from the database. Networks and kinds are in snapshot order, the
// order the all-networks list merges them. Without a snapshot it is empty
// ({}).
type DatasetLists struct {
	networks []*datasetNetwork
}

type datasetNetwork struct {
	slug  string
	kinds []*datasetKind
}

// datasetKind is one network's lists of one kind per range: its place lists
// by dimension (nil when the build has none) and its Coverage list, and the
// build and network its rows are scored in (placeScore; none without pg).
type datasetKind struct {
	kind     string
	places   [3][4]*placeIndex
	coverage [3]*coverageList
	pg       *Pages
	slug     string
}

// datasetLists is the lists of build pg: every published group of s that a
// network shows (a group of a provider kept off the site has none), by its
// network, in snapshot order.
func datasetLists(pg *Pages, s *snap.Snapshot, entries []*entry) *DatasetLists {
	out := &DatasetLists{}
	if s == nil {
		return out
	}
	slugOf := map[string]string{}
	for _, e := range entries {
		if e.P.Framework != nil {
			slugOf[e.P.Framework.Provider] = e.P.Slug
		}
	}
	networks, kinds := map[string]*datasetNetwork{}, map[string]bool{}
	for i := range s.Products {
		p := &s.Products[i]
		slug, kind := slugOf[p.Provider], proxyKind(p.Kind)
		if slug == "" || kinds[slug+"/"+kind] {
			continue
		}
		n := networks[slug]
		if n == nil {
			n = &datasetNetwork{slug: slug}
			networks[slug] = n
			out.networks = append(out.networks, n)
		}
		kinds[slug+"/"+kind] = true
		n.kinds = append(n.kinds, pg.datasetKind(slug, kind))
	}
	return out
}

// datasetKind is a network kind's lists of every range.
func (pg *Pages) datasetKind(slug, kind string) *datasetKind {
	k := &datasetKind{kind: kind, pg: pg, slug: slug}
	for r, rng := range Ranges {
		for d, dimension := range placeDimensions {
			k.places[r][d] = pg.lists[listKey{slug + "/" + kind, rng.ID, dimension}]
		}
		k.coverage[r] = pg.coverage[listKey{slug + "/" + kind, rng.ID, "coverage"}]
	}
	return k
}

// Repeated is d with its networks n times over, each copy after the first
// under the slugs "<slug>-<copy>": the memory tests' stand-in for more
// networks than a test build can hold. The copies share d's lists.
func (d *DatasetLists) Repeated(n int) *DatasetLists {
	out := &DatasetLists{}
	for c := range n {
		for _, nw := range d.networks {
			cp := *nw
			if c > 0 {
				cp.slug += "-" + strconv.Itoa(c)
			}
			out.networks = append(out.networks, &cp)
		}
	}
	return out
}

func (d *DatasetLists) MarshalJSON() ([]byte, error) {
	var b bytes.Buffer
	w := bufio.NewWriter(&b)
	if err := d.write(w); err != nil {
		return nil, err
	}
	err := w.Flush()
	return b.Bytes(), err
}

// write streams d as JSON, one list at a time.
func (d *DatasetLists) write(w *bufio.Writer) error {
	w.WriteByte('{') //nolint:errcheck // bufio reports the first error at Flush
	for i, n := range d.networks {
		writeKey(w, i, n.slug)
		w.WriteByte('{') //nolint:errcheck
		for j, k := range n.kinds {
			writeKey(w, j, k.kind)
			if err := k.write(w); err != nil {
				return err
			}
		}
		w.WriteByte('}') //nolint:errcheck
	}
	return w.WriteByte('}')
}

// writeKey writes an object member's key, after a comma unless it is the
// first (i == 0).
func writeKey(w *bufio.Writer, i int, key string) {
	if i > 0 {
		w.WriteByte(',') //nolint:errcheck
	}
	b, _ := json.Marshal(key) // a string always encodes
	w.Write(b)                //nolint:errcheck
	w.WriteByte(':')          //nolint:errcheck
}

// datasetList is one list of the file: its row and page counts and its first
// page.
type datasetList struct {
	Total int   `json:"total"`
	Pages int   `json:"pages"`
	Rows  []any `json:"rows"`
}

// write writes one network kind's ranges, each with its dimensions.
func (k *datasetKind) write(w *bufio.Writer) error {
	w.WriteByte('{') //nolint:errcheck
	for r, rng := range Ranges {
		writeKey(w, r, rng.ID)
		w.WriteByte('{') //nolint:errcheck
		for i, dimension := range datasetDimensions {
			writeKey(w, i, dimension)
			var l datasetList
			if dimension == "city_asn" {
				l = coverageFirst(k.coverage[r])
			} else {
				l = placeFirst(k.places[r][i], dimension, k.scorer(rng.ID, dimension))
			}
			if err := writeValue(w, l); err != nil {
				return err
			}
		}
		w.WriteByte('}') //nolint:errcheck
	}
	return w.WriteByte('}')
}

// scorer is the success score of the kind's rows of a range's dimension;
// nil without a build.
func (k *datasetKind) scorer(rangeID, dimension string) func(PlaceRow) *model.MetricScore {
	if k.pg == nil {
		return nil
	}
	return func(r PlaceRow) *model.MetricScore { return k.pg.placeScore(k.slug, k.kind, rangeID, dimension, r) }
}

// placeFirst is a place list's counts and first page (the rows List serves
// for page 1 without a query), each row with its success score when score
// is set (datasetScore); none when the build has no such list.
func placeFirst(x *placeIndex, dimension string, score func(PlaceRow) *model.MetricScore) datasetList {
	out := datasetList{Rows: []any{}}
	if x == nil {
		return out
	}
	out.Total, out.Pages = x.total, (x.total+PageSize-1)/PageSize
	for _, r := range x.first {
		row := placeArray(dimension, r)
		if score != nil {
			row = append(row, datasetScore(score(r)))
		}
		out.Rows = append(out.Rows, row)
	}
	return out
}

// coverageFirst is a Coverage list's counts and first page; none when the
// build has no such list.
func coverageFirst(c *coverageList) datasetList {
	out := datasetList{Rows: []any{}}
	if c == nil || c.first == nil {
		return out
	}
	out.Total, out.Pages = c.first.Total, c.first.Pages
	for _, r := range c.first.Rows {
		out.Rows = append(out.Rows, r)
	}
	return out
}

// datasetScore is a place row's success score as the dataset writes it, its
// status first: [status, score, rank, rankedOf] when ranked, else [status]
// (its score, rank and rankedOf are then null): a list's first page holds
// many unranked rows.
func datasetScore(s *model.MetricScore) any {
	if s.Status == scoreRanked {
		return [...]any{s.Status, s.Score, s.Rank, s.RankedOf}
	}
	return [...]any{s.Status}
}

// placeArray is a compact place row as the dataset writes it: the fields its
// dimension's place rows show, in their order, without the derived key and
// success rate and the fields the dimension never has (placeFirst adds the
// success score last).
func placeArray(dimension string, p PlaceRow) []any {
	switch dimension {
	case "country":
		return []any{p.CountryCode, p.Country, p.IpsSum, p.Requests, p.Successes, p.LatencyP50}
	case "region":
		return []any{p.CountryCode, p.Country, p.RegionCode, p.Region, p.IpsSum, p.Requests, p.Successes, p.LatencyP50}
	case "city":
		return []any{p.CountryCode, p.Country, p.RegionCode, p.Region, p.City, p.IpsSum, p.Requests, p.Successes, p.LatencyP50}
	}
	return []any{p.Asn, p.AsnName, p.IpsSum, p.Requests, p.Successes, p.LatencyP50}
}
