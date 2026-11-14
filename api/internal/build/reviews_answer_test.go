package build

import (
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// pagedInputs is a two-network build (net00, net01; residential) without
// site records.
func pagedInputs(t *testing.T) *store.Inputs {
	t.Helper()
	in, err := testscale.Inputs(testscale.Shape{Networks: 2, Countries: 1, Exits: 3, Tail: 4})
	if err != nil {
		t.Fatal(err)
	}
	return in
}

// storedPath is the stored path key gives for a request to target, a
// /v1/networks/{slug}/... URL.
func storedPath(key func(*http.Request) (string, error), target string) (string, error) {
	r := httptest.NewRequest(http.MethodGet, target, nil)
	r.SetPathValue("slug", strings.Split(r.URL.Path, "/")[3])
	return key(r)
}

// cut is the body the answer name cuts from what res holds for a request to
// target, nil when none.
func cut[T any](t *testing.T, res *Result, name string, target string) *T {
	t.Helper()
	a := answers[name]
	path, err := storedPath(a.Key, target)
	if err != nil {
		t.Fatalf("%s: %v", target, err)
	}
	body, _ := a.Cut(res.Pages.Held[name], path)
	ans, _ := body.(*T)
	return ans
}

func badRequest(t *testing.T, key func(*http.Request) (string, error), cases map[string]string) {
	t.Helper()
	for target, want := range cases {
		var bad APIError
		if _, err := storedPath(key, target); !errors.As(err, &bad) || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: %v, want %q", target, err, want)
		}
	}
}

// 45 published reviews of net00 are three pages, newest first; every filter
// is paged from the full list, and the counts cover every published review.
func TestFortyFiveReviewsAreThreePagesWithFilters(t *testing.T) {
	t.Parallel()
	in := pagedInputs(t)
	verifications := []string{"run_verified", "invoice_verified", "unverified"}
	for i := range 45 {
		in.Reviews = append(in.Reviews, store.ReviewRow{ID: fmt.Sprintf("r%02d", i), ProviderSlug: "net00", Rating: int16(i%5 + 1),
			Verification: verifications[i%3], PostedAt: hoursBefore(i)})
	}
	in.Reviews = append(in.Reviews, store.ReviewRow{ID: "held", ProviderSlug: "net00", Rating: 1, Verification: "pending", PostedAt: hoursBefore(0)},
		store.ReviewRow{ID: "other", ProviderSlug: "net01", Rating: 3, Verification: "unverified", PostedAt: hoursBefore(50)})
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	page := func(target string) *ReviewsAnswer { return cut[ReviewsAnswer](t, res, "reviews", target) }
	first, last := page("/v1/networks/net00/reviews"), page("/v1/networks/net00/reviews?page=3")
	if first == nil || last == nil || first.Total != 45 || first.Pages != 3 || len(first.Rows) != 20 || len(last.Rows) != 5 || last.Page != 3 {
		t.Fatalf("pages: %+v / %+v", first, last)
	}
	if first.Rows[0].ID != "r00" || first.Rows[0].Verification != "run_verified" || last.Rows[4].ID != "r44" || first.Rating != nil || first.Verified != nil {
		t.Errorf("first page %+v, last row %s", first.Rows[0], last.Rows[4].ID)
	}
	if c := first.Counts; c.Published != 45 || c.Verified != 30 || c.RunVerified != 15 || c.ByRating != [5]int{9, 9, 9, 9, 9} {
		t.Errorf("counts %+v", c)
	}
	if page("/v1/networks/net00/reviews?page=4") != nil || page("/v1/networks/gone/reviews") != nil {
		t.Error("a fourth page, or an unknown network's")
	}
	if _, found := cutReviews(res.Pages.Held["reviews"], "/v1/reviews/net00"); found || len(res.Pages.Held["reviews"]) != 3 {
		t.Errorf("a short path was cut, or the lists held are %v", res.Pages.Held["reviews"])
	}
	for target, want := range map[string]string{
		"/v1/networks/net00/reviews?rating=5":              "9 1 r04",
		"/v1/networks/net00/reviews?verified=run":          "15 1 r00",
		"/v1/networks/net00/reviews?verified=any&page=2":   "30 2 r30",
		"/v1/networks/net00/reviews?verified=invoice":      "15 1 r01",
		"/v1/networks/net00/reviews?rating=5&verified=run": "3 1 r09",
		"/v1/networks/net01/reviews?rating=3":              "1 1 other",
		"/v1/networks/all/reviews?page=3":                  "46 3 r40",
	} {
		ans := page(target)
		if ans == nil || len(ans.Rows) == 0 || fmt.Sprintf("%d %d %s", ans.Total, ans.Pages, ans.Rows[0].ID) != want {
			t.Errorf("%s: %+v, want %s", target, ans, want)
		}
	}
	if ans := page("/v1/networks/net01/reviews?rating=5&verified=invoice"); ans == nil || ans.Total != 0 || ans.Pages != 1 || ans.Rows == nil ||
		*ans.Rating != 5 || *ans.Verified != "invoice" || ans.Counts.Published != 1 {
		t.Errorf("empty filter: %+v", ans)
	}
}

func TestTheReviewsKeyRefusesBadQueries(t *testing.T) {
	t.Parallel()
	if got, err := storedPath(reviewsKey, "/v1/networks/net00/reviews?verified=any&rating=2&page=7"); got != "/v1/reviews/net00/2/any/7" || err != nil {
		t.Errorf("%q %v", got, err)
	}
	badRequest(t, reviewsKey, map[string]string{"/v1/networks/net00/reviews?rating=6": " name:rating value:", "/v1/networks/net00/reviews?rating=all": " name:rating value:",
		"/v1/networks/net00/reviews?verified=all": " name:verified value:", "/v1/networks/net00/reviews?page=0": " name:page value:",
		"/v1/networks/net00/reviews?page=x": " name:page value:", "/v1/networks/net00/reviews?kind=residential": "unknown_parameter map[name:",
		"/v1/networks/Net!/reviews": "invalid_parameter map[format:slug name:"})
}
