package testscale

import (
	"fmt"
	"hash/fnv"
)

// AddSuccessScores gives every product window the success scores of
// SUCCESS-SCORE-CALC-1 at their full width: each network target's
// scores.success and success_overall over every target of the window, with
// scores of 17 significant digits, as 1 + 9 × (M − r) / (M − 1) gives them,
// and each one's previous_score (SUCCESS-SCORE-DELTA-1): the score of the
// next rank, also at full width, so each delta is too; null on the first
// product, as with no previous build. A network target also gets its
// challenge and block scores (CHALLENGE-BLOCK-TIE-1), ranked at the same full
// width, and the window its challenge_overall (CHALLENGE-OVERALL-1), ranked
// the other way round, without a previous score.
func AddSuccessScores(doc map[string]any) error {
	for i, p := range doc["products"].([]any) {
		windows, ok := p.(map[string]any)["windows"].(map[string]any)
		if !ok {
			return fmt.Errorf("product %d has no windows", i)
		}
		rank := float64(i%18 + 1)
		score := 1 + 9*(18-rank)/17
		var previous any = 1 + 9*(18-(float64(i%17)+2))/17
		if i == 0 {
			previous = nil
		}
		for _, w := range windows {
			addSuccessScores(w.(map[string]any), rank, score, previous)
		}
	}
	return nil
}

func addSuccessScores(w map[string]any, rank, score float64, previous any) {
	targets := w["targets"].(map[string]any)
	scores, weights := map[string]any{}, map[string]any{}
	for id, entry := range targets {
		e := entry.(map[string]any)
		success := map[string]any{"rate": 0.9172413793103448, "score": score, "rank": rank, "ranked_of": 18,
			"status": "ranked", "previous_score": previous}
		if b, browser := e["browser"].(map[string]any); !browser {
			ranked := func(rate float64) map[string]any {
				return map[string]any{"rate": rate, "score": score, "rank": rank, "ranked_of": 18, "status": "ranked"}
			}
			e["scores"] = map[string]any{"success": success, "challenge": ranked(0.0123456789012345),
				"block": ranked(0.0234567890123456)}
		} else if s, ok := b["scores"].(map[string]any); ok && s["success"] != nil {
			s["success"].(map[string]any)["previous_score"] = previous
		}
		scores[id], weights[id] = score, 1
	}
	w["success_overall"] = map[string]any{"score": score, "rank": rank, "ranked_of": 18, "status": "ranked",
		"targets": scores, "weights": weights, "previous_score": previous}
	// Each point with requests reads the window's score (SUCCESS-POINTS-1: a steady network).
	series, _ := w["series"].([]any)
	for _, point := range series {
		if pt := point.(map[string]any); pt["completed"] != nil && pt["completed"] != 0.0 {
			pt["success_score"] = map[string]any{"score": score, "status": "ranked"}
		}
	}
	challengeRank := 19 - rank
	challenge, challenges := 1+9*(18-challengeRank)/17, map[string]any{}
	for id := range scores {
		challenges[id] = challenge
	}
	w["challenge_overall"] = map[string]any{"score": challenge, "rank": challengeRank, "ranked_of": 18, "status": "ranked",
		"targets": challenges, "weights": weights}
}

// addGaps leaves every seventh series point of the group's windows, and one
// more, without a success rate or latency, both placed by a hash of the
// group's id, so the networks' histories do not share their point times:
// their home curves each have their own labels, as in production (build 17:
// 37 label arrays for 18 networks).
func addGaps(g map[string]any) {
	h := fnv.New32a()
	_, _ = h.Write([]byte(fmt.Sprint(g["id"])))
	sum := int(h.Sum32())
	windows, _ := g["windows"].(map[string]any)
	for _, w := range windows {
		series, _ := w.(map[string]any)["series"].([]any)
		for k, point := range series {
			if pt := point.(map[string]any); (k+sum)%7 == 0 || k == sum%len(series) {
				pt["success_rate"], pt["latency_ms"] = nil, nil
			}
		}
	}
}
