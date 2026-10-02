package server

import (
	"bytes"
	"compress/gzip"
	"container/list"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strconv"
	"strings"
	"sync"

	"github.com/andybalholm/brotli"
)

// Cache-Control values.
const (
	CacheStatic = "public, max-age=60, stale-while-revalidate=600"
	CacheEvents = "public, max-age=15, stale-while-revalidate=60"
	// CacheVersion is short: /v1/version is how open pages notice new data.
	CacheVersion = "public, max-age=5"
	CacheNone    = "no-store"
)

// Rendered is one immutable response body with its precompressed variants.
type Rendered struct {
	Status       int
	Identity     []byte
	Gzip         []byte
	Brotli       []byte
	ETag         string // quoted, identity representation
	CacheControl string
	// identity/gzip/brotli Content-Length values, preformatted.
	lenIdentity, lenGzip, lenBrotli string
}

// Level picks compression effort: precomputed bodies get the best ratio,
// per-request (search, events) bodies a fast setting.
type Level int

const (
	LevelBest Level = iota
	LevelFast
)

func compress(body []byte, level Level) (gz, br []byte) {
	var g bytes.Buffer
	gl := gzip.BestCompression
	// Brotli 11 is ~3x slower than 10 for ~4% smaller output; above 512 KB
	// (the full dataset is ~1.9 MB) quality 10 with a 16 MB window keeps a
	// rebuild well under a second.
	bopts := brotli.WriterOptions{Quality: brotli.BestCompression, LGWin: 22}
	switch {
	case level == LevelFast:
		gl, bopts.Quality = gzip.DefaultCompression, 5
	case len(body) > 512<<10:
		bopts = brotli.WriterOptions{Quality: 10, LGWin: 24}
	}
	gw, _ := gzip.NewWriterLevel(&g, gl)
	gw.Write(body) //nolint:errcheck // bytes.Buffer cannot fail
	gw.Close()     //nolint:errcheck
	var b bytes.Buffer
	bw := brotli.NewWriterOptions(&b, bopts)
	bw.Write(body) //nolint:errcheck
	bw.Close()     //nolint:errcheck
	return g.Bytes(), b.Bytes()
}

// Marshal encodes v as compact JSON.
func Marshal(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	out := buf.Bytes()
	return out[:len(out)-1], nil // drop Encode's trailing newline
}

// Render encodes and precompresses a value.
func Render(status int, v any, cacheControl string, level Level) (*Rendered, error) {
	body, err := Marshal(v)
	if err != nil {
		return nil, err
	}
	return RenderBytes(status, body, cacheControl, level), nil
}

// RenderBytes precompresses an already encoded body.
func RenderBytes(status int, body []byte, cacheControl string, level Level) *Rendered {
	sum := sha256.Sum256(body)
	gz, br := compress(body, level)
	return &Rendered{Status: status, Identity: body, Gzip: gz, Brotli: br, ETag: `"` + hex.EncodeToString(sum[:]) + `"`,
		CacheControl: cacheControl, lenIdentity: strconv.Itoa(len(body)), lenGzip: strconv.Itoa(len(gz)), lenBrotli: strconv.Itoa(len(br))}
}

// Encoding is a negotiated content coding.
type Encoding int

const (
	Identity Encoding = iota
	Gzip
	Brotli
)

// Variant returns the body, Content-Encoding, ETag and length of a coding.
// Each coding has its own strong ETag, since the bytes differ.
func (r *Rendered) Variant(e Encoding) (body []byte, encoding, etag, length string) {
	switch e {
	case Brotli:
		return r.Brotli, "br", r.ETag[:len(r.ETag)-1] + `-br"`, r.lenBrotli
	case Gzip:
		return r.Gzip, "gzip", r.ETag[:len(r.ETag)-1] + `-gz"`, r.lenGzip
	}
	return r.Identity, "", r.ETag, r.lenIdentity
}

// Matches reports whether an If-None-Match header names any representation of
// this body (weak comparison, as RFC 9110 requires for If-None-Match).
func (r *Rendered) Matches(header string) bool {
	if header == "" {
		return false
	}
	base := r.ETag[1 : len(r.ETag)-1]
	for _, tag := range strings.Split(header, ",") {
		tag = strings.TrimSpace(tag)
		if tag == "*" {
			return true
		}
		tag = strings.TrimPrefix(tag, "W/")
		tag = strings.Trim(tag, `"`)
		if tag == base || tag == base+"-gz" || tag == base+"-br" {
			return true
		}
	}
	return false
}

// Negotiate picks br, then gzip, then identity from Accept-Encoding.
func Negotiate(header string) Encoding {
	if header == "" {
		return Identity
	}
	br, gz, star := -1.0, -1.0, -1.0
	for _, part := range strings.Split(header, ",") {
		name, params, _ := strings.Cut(strings.TrimSpace(part), ";")
		q := 1.0
		if params != "" {
			for _, p := range strings.Split(params, ";") {
				k, v, _ := strings.Cut(strings.TrimSpace(p), "=")
				if strings.EqualFold(k, "q") {
					if f, err := strconv.ParseFloat(v, 64); err == nil {
						q = f
					}
				}
			}
		}
		switch strings.ToLower(strings.TrimSpace(name)) {
		case "br":
			br = q
		case "gzip", "x-gzip":
			gz = q
		case "*":
			star = q
		}
	}
	if br < 0 {
		br = star
	}
	if gz < 0 {
		gz = star
	}
	switch {
	case br > 0 && br >= gz:
		return Brotli
	case gz > 0:
		return Gzip
	case br > 0:
		return Brotli
	}
	return Identity
}

// LRU is a small bounded cache of rendered responses.
type LRU struct {
	mu    sync.Mutex
	cap   int
	ll    *list.List
	items map[string]*list.Element
	hits  uint64
	miss  uint64
}

type lruEntry struct {
	key string
	val *Rendered
}

func NewLRU(capacity int) *LRU {
	return &LRU{cap: capacity, ll: list.New(), items: map[string]*list.Element{}}
}

func (c *LRU) Get(key string) (*Rendered, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if el, ok := c.items[key]; ok {
		c.ll.MoveToFront(el)
		c.hits++
		return el.Value.(*lruEntry).val, true
	}
	c.miss++
	return nil, false
}

func (c *LRU) Add(key string, v *Rendered) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if el, ok := c.items[key]; ok {
		el.Value.(*lruEntry).val = v
		c.ll.MoveToFront(el)
		return
	}
	c.items[key] = c.ll.PushFront(&lruEntry{key, v})
	for c.ll.Len() > c.cap {
		last := c.ll.Back()
		c.ll.Remove(last)
		delete(c.items, last.Value.(*lruEntry).key)
	}
}

// Len is the number of cached entries.
func (c *LRU) Len() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.ll.Len()
}

// Stats returns hits and misses.
func (c *LRU) Stats() (hits, misses uint64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.hits, c.miss
}
