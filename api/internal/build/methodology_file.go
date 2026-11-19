package build

import (
	"crypto/sha256"
	"encoding/hex"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/pyjson"
	"github.com/shifter-io/atlas/api/internal/store"
)

// The methodology files (METHODOLOGY-FILE-1, #1179): GET
// /v1/methodology/<id>.json serves the rules a methodology id stands for,
// the exact bytes the id is the SHA-256 of (api/README.md, "Methodology
// files"), so anyone can check the rules a snapshot was calculated with.

// MethodologyPrefix is the methodology files' route prefix.
const MethodologyPrefix = "/v1/methodology/"

// MethodologyPath is the path of methodology id's file.
func MethodologyPath(id string) string { return MethodologyPrefix + id + ".json" }

// NAMethodologyFileUnknown: a snapshot is published, but the API holds no
// rules whose SHA-256 is its methodology id (snapshot.methodologyUrl).
const NAMethodologyFileUnknown = "methodology_file_unknown"

// MethodologyFiles is the body of every methodology row whose canonical
// definition (pyjson.Canonical) hashes to its id, by its MethodologyPath. A
// row that does not (a definition the database cannot give back byte for
// byte, such as a float written in exponent form above 1e16) has no file.
func MethodologyFiles(rows []store.MethodologyRow) map[string][]byte {
	out := map[string][]byte{}
	for _, r := range rows {
		body, err := pyjson.Canonical(r.Definition)
		if err != nil {
			continue
		}
		sum := sha256.Sum256(body)
		if hex.EncodeToString(sum[:]) == r.ID {
			out[MethodologyPath(r.ID)] = body
		}
	}
	return out
}

// methodologyURL is the path of id's file, or null when files has none.
func methodologyURL(files map[string][]byte, id string) model.Str {
	if _, ok := files[MethodologyPath(id)]; !ok {
		return nil
	}
	return model.S(MethodologyPath(id))
}
