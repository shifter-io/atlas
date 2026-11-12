package testscale

import "fmt"

// AddSuccessScores gives every product window the success scores of
// SUCCESS-SCORE-CALC-1 at their full width: each network target's
// scores.success and success_overall over every target of the window, with
// scores of 17 significant digits, as 1 + 9 × (M − r) / (M − 1) gives them,
// and each one's previous_score (SUCCESS-SCORE-DELTA-1): the score of the
// next rank, also at full width, so each delta is too; null on the first
// product, as with no previous build.
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
			e["scores"] = map[string]any{"success": success}
		} else if s, ok := b["scores"].(map[string]any); ok && s["success"] != nil {
			s["success"].(map[string]any)["previous_score"] = previous
		}
		scores[id], weights[id] = score, 1
	}
	w["success_overall"] = map[string]any{"score": score, "rank": rank, "ranked_of": 18, "status": "ranked",
		"targets": scores, "weights": weights, "previous_score": previous}
}
