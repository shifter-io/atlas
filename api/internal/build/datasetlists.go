package build

import (
	"bufio"
	"bytes"
	"encoding/json"
	"strconv"

	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// listChunk is how many rows the dataset's lists encode at once.
const listChunk = 1000

// datasetDimensions are the dimensions of a network window in the dataset's
// lists, in the order they are written.
var datasetDimensions = [...]string{"country", "region", "city", "asn", "city_asn"}

// DatasetLists is the dataset file's `lists` (contract K11): in a v4 build,
// lists[slug][kind][range][dimension] holds every row of the network
// window's lists as an array (FIELDS.md gives each dimension's field order),
// so the file alone regenerates every list answer. It holds only the build's
// compact lists (store.NetList), never their rows: WriteDataset streams it
// one dimension at a time, listChunk rows at a time, and MarshalJSON gives the
// same bytes. Networks and kinds are in the order the all-networks list
// merges them (the snapshot's), so a reader that merges them in file order
// gets that list's latency choices. Before v4 it is empty ({}).
type DatasetLists struct {
	networks []*datasetNetwork
}

type datasetNetwork struct {
	slug  string
	kinds []*datasetKind
}

// datasetKind is one network's lists of one kind per range: the lists its
// place answers merge (srcs) and its Coverage list.
type datasetKind struct {
	kind     string
	srcs     [3][]placeSource
	coverage [3]*coverageList
}

// datasetLists is the lists of build pg: every published group of s that a
// network shows (a group of a provider kept off the site has none), by its
// network, in snapshot order.
func datasetLists(pg *Pages, s *snap.Snapshot, entries []*entry, row *store.SnapshotRow) *DatasetLists {
	out := &DatasetLists{}
	if !pg.v4 || s == nil {
		return out
	}
	slugOf := map[string]string{}
	for _, e := range entries {
		if e.P.Framework != nil {
			slugOf[e.P.Framework.Provider] = e.P.Slug
		}
	}
	networks, kinds := map[string]*datasetNetwork{}, map[string]*datasetKind{}
	for i := range s.Products {
		p := &s.Products[i]
		slug, kind := slugOf[p.Provider], proxyKind(p.Kind)
		if slug == "" {
			continue
		}
		n := networks[slug]
		if n == nil {
			n = &datasetNetwork{slug: slug}
			networks[slug] = n
			out.networks = append(out.networks, n)
		}
		k := kinds[slug+"/"+kind]
		if k == nil {
			k = &datasetKind{kind: kind}
			kinds[slug+"/"+kind] = k
			n.kinds = append(n.kinds, k)
			for r, rng := range Ranges {
				k.coverage[r] = pg.coverage[listKey{slug + "/" + kind, rng.ID, "coverage"}]
			}
		}
		for r, rng := range Ranges {
			w := p.Window(rng.ID)
			if l := netListOf(row, p, w); l != nil {
				k.srcs[r] = append(k.srcs[r], placeSource{list: l, scale: breakdownScale(w)})
			}
		}
	}
	return out
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

// write streams d as JSON.
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

// write writes one network kind's ranges, each with its dimensions.
func (k *datasetKind) write(w *bufio.Writer) error {
	w.WriteByte('{') //nolint:errcheck
	for r, rng := range Ranges {
		writeKey(w, r, rng.ID)
		w.WriteByte('{') //nolint:errcheck
		for i, dimension := range datasetDimensions {
			writeKey(w, i, dimension)
			rows := &rowWriter{w: w}
			if dimension == "city_asn" {
				k.coverageRows(r, rows)
			} else {
				for _, s := range k.srcs[r] {
					for j := range s.list.Len(dimension) {
						rows.add(placeArray(dimension, s.list.Row(dimension, j), s.scale))
					}
				}
			}
			if err := rows.close(); err != nil {
				return err
			}
		}
		w.WriteByte('}') //nolint:errcheck
	}
	return w.WriteByte('}')
}

// coverageRows adds range r's Coverage rows: those with IPs, most IPs first,
// as the Coverage answer writes them (with its country latency rank).
func (k *datasetKind) coverageRows(r int, rows *rowWriter) {
	c := k.coverage[r]
	if c == nil {
		return
	}
	for _, i := range c.list.ByIps("city_asn") {
		if c.list.Row("city_asn", int(i)).Ips > 0 {
			rows.add(c.row(i))
		}
	}
}

// placeArray is a compact place row as the dataset writes it: the fields its
// dimension's place rows show, in their order, without the derived key and
// success rate and the fields the dimension never has.
func placeArray(dimension string, r store.NetRow, scale Num) any {
	p, asn := netPlaceRow(r, scale)
	switch dimension {
	case "country":
		return [...]any{p.CountryCode, p.Country, p.IpsSum, p.Requests, p.Successes, p.LatencyP50}
	case "region":
		return [...]any{p.CountryCode, p.Country, p.RegionCode, p.Region, p.IpsSum, p.Requests, p.Successes, p.LatencyP50}
	case "city":
		return [...]any{p.CountryCode, p.Country, p.RegionCode, p.Region, p.City, p.IpsSum, p.Requests, p.Successes, p.LatencyP50}
	}
	return [...]any{asn, p.AsnName, p.IpsSum, p.Requests, p.Successes, p.LatencyP50}
}

// rowWriter writes one JSON array of rows, encoding listChunk rows at a time.
type rowWriter struct {
	w     *bufio.Writer
	chunk []any
	n     int
	err   error
}

func (rw *rowWriter) add(row any) {
	if rw.chunk = append(rw.chunk, row); len(rw.chunk) == listChunk {
		rw.flush()
	}
}

// flush writes the chunk's rows after those already written.
func (rw *rowWriter) flush() {
	if len(rw.chunk) == 0 || rw.err != nil {
		return
	}
	b, err := json.Marshal(rw.chunk)
	if err != nil {
		rw.err = err
		return
	}
	rw.w.WriteByte("[,"[min(rw.n, 1)]) //nolint:errcheck // "[" first, then ","
	rw.w.Write(b[1 : len(b)-1])        //nolint:errcheck
	rw.n += len(rw.chunk)
	clear(rw.chunk)
	rw.chunk = rw.chunk[:0]
}

// close writes the last rows and ends the array.
func (rw *rowWriter) close() error {
	rw.flush()
	if rw.err != nil {
		return rw.err
	}
	if rw.n == 0 {
		rw.w.WriteByte('[') //nolint:errcheck
	}
	return rw.w.WriteByte(']')
}
