package build

import (
	"bufio"
	"slices"
	"strings"
)

// DatasetPages is the dataset file's `pages`: the body of every per-page
// answer the site's pages draw, by the request that serves it, so the file
// alone regenerates each answer byte for byte (the server renders the same
// values). Keys are "/v1/networks", "/v1/rankings?range=<r>",
// "/v1/networks/<slug>?kind=<kind>" and
// "/v1/networks/<slug>/chart?kind=<kind>&range=<r>" (the all-networks answer
// under slug and kind "all"), and in a v4 build "/v1/countries" and
// "/v1/countries/<CC>" (contract K11: the country pages are pages of the
// site's design). The network lists' first pages are the dataset's lists
// (DatasetLists); further pages, the answers with q and the region and city
// pages are read from the API; search is not a page.
type DatasetPages map[string]any

// datasetPages is pg's page answers by request.
func datasetPages(pg *Pages) DatasetPages {
	out := DatasetPages{"/v1/networks": pg.Networks}
	if pg.v4 {
		out["/v1/countries"] = pg.Countries
		for cc, ans := range pg.Country {
			out["/v1/countries/"+cc] = ans
		}
	}
	for rangeID, ans := range pg.Rankings {
		out["/v1/rankings?range="+rangeID] = ans
	}
	for key, ans := range pg.Network {
		slug, kind, _ := strings.Cut(key, "/")
		out["/v1/networks/"+slug+"?kind="+kind] = ans
	}
	for key, ans := range pg.Charts {
		parts := strings.SplitN(key, "/", 3)
		out["/v1/networks/"+parts[0]+"/chart?kind="+parts[1]+"&range="+parts[2]] = ans
	}
	return out
}

// write streams p as encoding the map gives it (keys in order), one answer at
// a time.
func (p DatasetPages) write(w *bufio.Writer) error {
	keys := make([]string, 0, len(p))
	for k := range p {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	w.WriteByte('{') //nolint:errcheck // bufio reports the first error at Flush
	for i, k := range keys {
		writeKey(w, i, k)
		if err := writeValue(w, p[k]); err != nil {
			return err
		}
	}
	return w.WriteByte('}')
}
