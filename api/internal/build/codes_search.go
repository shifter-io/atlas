package build

// /v1/search (SEARCH-NEUTRAL-1, #981).

func init() {
	RegisterCodeSet(CodeSet{Name: "search_item_type", Source: CodesFromAPI, UsedIn: []string{"/v1/search items[].type"},
		Values: codes(SearchNetwork, "A network: the id is its slug.", SearchTarget, "A test target: the id is its target id.")})
}
