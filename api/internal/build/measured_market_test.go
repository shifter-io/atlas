package build

import (
	"encoding/json"
	"math"
	"testing"

	"github.com/ice-zeus/proxy-index-website/api/internal/model"
)

func TestMeasuredMarketCombinesLiveNetworksAndKeepsHistoryGaps(t *testing.T) {
	fleet := []*entry{
		{P: &model.Provider{Slug: "first", Source: model.SourceMeasured, PrimaryKind: "residential", Pool: model.PoolStats{UniqueIps24h: 10}, Trust: model.TrustSignals{ResidentialAuthenticity: 0.9}}},
		{P: &model.Provider{Slug: "second", Source: model.SourceMeasured, PrimaryKind: "residential", Pool: model.PoolStats{UniqueIps24h: 30}, Trust: model.TrustSignals{ResidentialAuthenticity: 1}}},
	}
	stats := map[string]map[string]*WindowStats{"first": {}, "second": {}}
	for _, r := range Ranges {
		stats["first"][r.ID] = &WindowStats{Requests: 100, UniqueIps: 10, LatencyP50: 100, SuccessRate: 0.8, CostPer1kSuccess: nan,
			Success: []model.SeriesPoint{{T: 1, V: 0.2}, {T: 0, V: nan}}, Latency: []model.LatencyPoint{{T: 1, P50: 100}}}
		stats["second"][r.ID] = &WindowStats{Requests: 300, UniqueIps: 30, LatencyP50: 300, SuccessRate: 0.9, CostPer1kSuccess: nan,
			Success: []model.SeriesPoint{{T: 1, V: 0.8}, {T: 0, V: nan}}, Latency: []model.LatencyPoint{{T: 1, P50: 300}}}
	}
	market, err := measuredMarket(fleet, stats, model.FleetTotals{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if math.Abs(market.Authenticity.F()-0.975) > 1e-12 {
		t.Fatalf("weighted authenticity: %v", market.Authenticity)
	}
	var views []FleetView
	if err := json.Unmarshal(market.Views, &views); err != nil {
		t.Fatal(err)
	}
	for _, v := range views {
		if v.Fleet.Requests != 400 || v.Fleet.UniqueIps != 40 || v.Fleet.MedianLatency != 200 {
			t.Errorf("combined measured window: %+v", v.Fleet)
		}
		if math.Abs(v.Fleet.Success[0].V.F()-0.65) > 1e-12 {
			t.Errorf("weighted measured history: %+v", v.Fleet.Success)
		}
		if v.Fleet.Success[1].V.OK() || v.Fleet.Latency[1].V.OK() || v.Fleet.BandwidthGb.OK() {
			t.Errorf("missing measurements fabricated: %+v", v.Fleet)
		}
		if v.Fastest.Provider.Slug != "first" || v.MostReliable.Provider.Slug != "second" {
			t.Errorf("measured leader picks disagree")
		}
	}
}

func TestMiniChartObservedPeriod(t *testing.T) {
	points := []model.SeriesPoint{{T: 100, V: nan}, {T: 20, V: 100}, {T: 15, V: nan}, {T: 10, V: 200}, {T: 0, V: nan}}
	got := trimUnobserved(points)
	if len(got) != 3 || got[0].T != 20 || got[2].T != 10 || got[1].V.OK() {
		t.Fatalf("observed chart period or interior gap lost: %+v", got)
	}
	if len(trimUnobserved([]model.SeriesPoint{{V: nan}})) != 0 {
		t.Fatal("invented data")
	}
	if len(trimUnobserved(points[1:2])) != 1 {
		t.Fatal("first sample lost")
	}
}
