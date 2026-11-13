package build

// The column names of the place lists' compact rows (PLACE-CODES-1): a
// Coverage answer's and every dataset-file list's columns name its rows'
// positions, so a reader takes each value by its name.

func init() {
	RegisterCodeSet(CodeSet{Name: "place_column", Source: CodesFromAPI,
		UsedIn: []string{"/v1/networks/{slug}/coverage columns[]", "/v1/dataset lists.<id>.<id>.<id>.<id>.columns[]"},
		Values: codes(
			"countryCode", "The place's ISO 3166-1 country code.",
			"country", "The country's English name; leaving with #992 (write the name from countryCode).",
			"regionCode", "The region's code within its country: with countryCode, the ISO 3166-2 code (US and NY: US-NY).",
			"region", "The region's name, as the location source writes it.",
			"city", "The city's name, as the location source writes it.",
			"asn", "The network operator's AS number.",
			"asnName", "The network operator's name, as the location source writes it.",
			"usageType", "The network operator's usage type, as the location source writes it.",
			"ips", "The network's distinct exit addresses there.",
			"ipsSum", "The network's distinct exit addresses there, summed over its product groups.",
			"requests", "The completed requests there.",
			"successes", "The successful requests there.",
			"latencyP50", "The median latency there, in ms.",
			"latencyRank", "The network's latency rank in the row's country among the networks of its kind.",
			"latencyPeers", "The networks ranked in the row's country.",
			"ipsRank", "The network's rank by distinct exit addresses at the place among the networks of its kind.",
			"ipsPeers", "The networks with addresses at the place.",
			"successScore", "The row's success score: [status], or [status, score, rank, rankedOf] when ranked.")})
}
