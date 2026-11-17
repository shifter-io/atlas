package build

import (
	"cmp"
	"fmt"
	"net/http"
	"slices"
	"strconv"
	"strings"

	"github.com/shifter-io/atlas/api/internal/model"
)

// /v1/networks/{slug}/reviews?rating=&verified=&page= (api/answers/reviews.md):
// every published review of a network, filtered and paged, where the network
// answer holds only the newest RecordCap. The build holds each network's
// list; a page is cut from it on first request. "all" is every network's.
func init() {
	RegisterAnswer(PageAnswer{
		Name:    "reviews",
		Route:   "GET /v1/networks/{slug}/reviews",
		Key:     reviewsKey,
		Build:   buildReviews,
		Cut:     cutReviews,
		Samples: []string{"/v1/networks/all/reviews", "/v1/networks/all/reviews?rating=5&verified=run&page=1"},
	})
}

// RecordPageSize is how many rows a page of a paged record answer holds.
const RecordPageSize = 20

// reviewVerified is the verification states each verified= value keeps;
// "all" (no verified=) keeps every one.
var reviewVerified = map[string][]string{"all": nil, "any": {"invoice_verified", "run_verified"},
	"invoice": {"invoice_verified"}, "run": {"run_verified"}}

var reviewRatings = []string{"all", "1", "2", "3", "4", "5"}

// ReviewCounts counts a network's published reviews, whatever the filter;
// ByRating[i] is the reviews rated i+1.
type ReviewCounts struct {
	Published   int    `json:"published"`
	Verified    int    `json:"verified"`
	RunVerified int    `json:"runVerified"`
	ByRating    [5]int `json:"byRating"`
}

// ReviewsAnswer is one page of a network's published reviews, newest first.
type ReviewsAnswer struct {
	GeneratedAt string             `json:"generatedAt"`
	Network     string             `json:"network"`
	Rating      *int               `json:"rating"`
	Verified    *string            `json:"verified"`
	Page        int                `json:"page"`
	Pages       int                `json:"pages"`
	Total       int                `json:"total"`
	Counts      ReviewCounts       `json:"counts"`
	Rows        []model.UserReview `json:"rows"`
}

// reviewList is what the build holds per network: its published reviews,
// newest first, and their counts.
type reviewList struct {
	generatedAt, slug string
	rows              []model.UserReview
	counts            ReviewCounts
}

func reviewsKey(r *http.Request) (string, error) {
	slug, err := PathSlug(r)
	if err != nil {
		return "", err
	}
	q, err := Query(r, "rating", "verified", "page")
	if err != nil {
		return "", err
	}
	rating, verified := q.Get("rating"), q.Get("verified")
	if _, ok := reviewVerified[verified]; verified != "" && (!ok || verified == "all") {
		return "", InvalidValue("verified", verified, "invoice", "run", "any")
	}
	if rating != "" && (!slices.Contains(reviewRatings, rating) || rating == "all") {
		return "", InvalidValue("rating", rating, reviewRatings[1:]...)
	}
	rating, verified = cmp.Or(rating, "all"), cmp.Or(verified, "all")
	page, err := QueryPage(q)
	return fmt.Sprintf("/v1/reviews/%s/%s/%s/%d", slug, rating, verified, page), err
}

func buildReviews(c *Context) (map[string]any, error) {
	out := map[string]any{}
	for slug, rows := range recordScopes(c, func(d *ProviderDetail) []model.UserReview { return d.Reviews }, publishedReviews(c.Dataset.Reviews)) {
		out["/v1/reviews/"+slug] = &reviewList{generatedAt: c.GeneratedAt, slug: slug, rows: rows, counts: reviewCounts(rows)}
	}
	return out, nil
}

// cutReviews is the page at path (reviewsKey's) of its network's list.
func cutReviews(held map[string]any, path string) (any, bool) {
	parts := strings.Split(path, "/") // "", v1, reviews, slug, rating, verified, page
	if len(parts) != 7 {
		return nil, false
	}
	list, ok := held["/v1/reviews/"+parts[3]].(*reviewList)
	if !ok {
		return nil, false
	}
	verified, keep := parts[5], reviewVerified[parts[5]]
	stars, _ := strconv.Atoi(parts[4]) // 0 for all
	n, _ := strconv.Atoi(parts[6])
	kept := filterRows(list.rows, func(r model.UserReview) bool {
		return (stars == 0 || int(r.Rating.F()) == stars) && (keep == nil || slices.Contains(keep, r.Verification))
	})
	page, pages, ok := cutPage(kept, n)
	if !ok {
		return nil, false
	}
	ans := &ReviewsAnswer{GeneratedAt: list.generatedAt, Network: list.slug, Page: n, Pages: pages, Total: len(kept),
		Counts: list.counts, Rows: page}
	if stars != 0 {
		ans.Rating = &stars
	}
	if verified != "all" {
		ans.Verified = model.S(verified)
	}
	return ans, true
}

func publishedReviews(all []model.UserReview) []model.UserReview {
	return filterRows(all, func(r model.UserReview) bool { return r.Verification != "pending" })
}

func reviewCounts(rows []model.UserReview) ReviewCounts {
	c := ReviewCounts{Published: len(rows)}
	for _, r := range rows {
		if slices.Contains(reviewVerified["any"], r.Verification) {
			c.Verified++
		}
		if r.Verification == "run_verified" {
			c.RunVerified++
		}
		if i := int(r.Rating.F()) - 1; i >= 0 && i < 5 {
			c.ByRating[i]++
		}
	}
	return c
}

// recordScopes is every network's records (its detail's, newest first) by
// slug, and all of them under AllNetworks unless a network has that slug.
func recordScopes[T any](c *Context, of func(*ProviderDetail) []T, all []T) map[string][]T {
	out := map[string][]T{}
	for slug, d := range c.Details {
		out[slug] = of(d)
	}
	if _, ok := out[AllNetworks]; !ok {
		out[AllNetworks] = all
	}
	return out
}

// cutPage is page n of rows, RecordPageSize each, and the number of pages
// (at least one: an empty first page when there are no rows); ok is false
// past the last page.
func cutPage[T any](rows []T, n int) (page []T, pages int, ok bool) {
	pages = max(1, (len(rows)+RecordPageSize-1)/RecordPageSize)
	if n < 1 || n > pages {
		return nil, pages, false
	}
	return rows[(n-1)*RecordPageSize : min(len(rows), n*RecordPageSize)], pages, true
}

func filterRows[T any](rows []T, keep func(T) bool) []T {
	out := []T{}
	for _, r := range rows {
		if keep(r) {
			out = append(out, r)
		}
	}
	return out
}
