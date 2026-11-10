package store

import "strings"

// matcher is which words of a query each place and network name holds,
// looked up once per name, not per row: a City x ASN list repeats its names
// over many rows (COVERAGE-SEARCH-2). Bit w of a mask is word w; maskKnown marks
// a name looked up. A query of more words than a mask holds is matched row
// by row.
type matcher struct {
	words []string
	all   uint64
	texts [2][]string
	masks [2][]uint64
}

const maskKnown = 1 << 63

func newMatcher(names netNames, words []string) *matcher {
	m := &matcher{words: words, all: 1<<len(words) - 1, texts: [2][]string{names.placeText, names.networkText}}
	if len(words) < 63 {
		m.masks = [2][]uint64{make([]uint64, len(names.placeText)), make([]uint64, len(names.networkText))}
	}
	return m
}

func (m *matcher) holds(r *netRow) bool {
	if m.masks[0] == nil {
		return m.holdsEach(r)
	}
	return (m.mask(0, r.place)|m.mask(1, r.network))&m.all == m.all
}

// mask is the words name i of kind k (0 a place, 1 a network) holds.
func (m *matcher) mask(k int, i int32) uint64 {
	if i < 0 {
		return 0
	}
	if v := m.masks[k][i]; v != 0 {
		return v
	}
	v := uint64(maskKnown)
	for w, word := range m.words {
		if strings.Contains(m.texts[k][i], word) {
			v |= 1 << w
		}
	}
	m.masks[k][i] = v
	return v
}

func (m *matcher) holdsEach(r *netRow) bool {
	place, network := "", ""
	if r.place >= 0 {
		place = m.texts[0][r.place]
	}
	if r.network >= 0 {
		network = m.texts[1][r.network]
	}
	for _, w := range m.words {
		if !strings.Contains(place, w) && !strings.Contains(network, w) {
			return false
		}
	}
	return true
}
