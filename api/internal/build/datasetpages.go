package build

import "strings"

// DatasetPages is the dataset file's `pages`: the body of every per-page
// answer the site's pages draw, by the request that serves it, so the file
// alone regenerates each answer byte for byte (the server renders the same
// values). Keys are "/v1/networks", "/v1/rankings?range=<r>",
// "/v1/networks/<slug>?kind=<kind>" and
// "/v1/networks/<slug>/chart?kind=<kind>&range=<r>" (the all-networks answer
// under slug and kind "all"). The country answers, the long place lists and
// search are not pages of the site's design (API-DATA-DESIGN-1), so they are
// not in it.
type DatasetPages map[string]any

// datasetPages is pg's page answers by request.
func datasetPages(pg *Pages) DatasetPages {
	out := DatasetPages{"/v1/networks": pg.Networks}
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
