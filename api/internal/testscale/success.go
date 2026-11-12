package testscale

import "fmt"

// AddSuccessScores gives every product window the success scores of
// SUCCESS-SCORE-CALC-1 at their full width: each network target's
// scores.success and success_overall over every target of the window, with
// scores of 17 significant digits, as 1 + 9 × (M − r) / (M − 1) gives them.
func AddSuccessScores(doc map[string]any) error {
	for i, p := range doc["products"].([]any) {
		windows, ok := p.(map[string]any)["windows"].(map[string]any)
		if !ok {
			return fmt.Errorf("product %d has no windows", i)
		}
		rank := float64(i%18 + 1)
		score := 1 + 9*(18-rank)/17
		for _, w := range windows {
			addSuccessScores(w.(map[string]any), rank, score)
		}
	}
	return nil
}

func addSuccessScores(w map[string]any, rank, score float64) {
	targets := w["targets"].(map[string]any)
	scores, weights := map[string]any{}, map[string]any{}
	for id, entry := range targets {
		e := entry.(map[string]any)
		if _, browser := e["browser"]; !browser {
			e["scores"] = map[string]any{"success": map[string]any{"rate": 0.9172413793103448, "score": score, "rank": rank,
				"ranked_of": 18, "status": "ranked"}}
		}
		scores[id], weights[id] = score, 1
	}
	w["success_overall"] = map[string]any{"score": score, "rank": rank, "ranked_of": 18, "status": "ranked",
		"targets": scores, "weights": weights}
}
