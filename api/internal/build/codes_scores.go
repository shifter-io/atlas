package build

// The scores: statuses, bases, components and grades, and the reasons of a
// null field (CODES-CATALOG-1).

func init() {
	RegisterCodeSet(CodeSet{Name: "score_status", Source: CodesFromAPI, UsedIn: []string{"* **.successScore.status",
		"* **.scores.<id>.status", "* **.statuses[]", "* **.browserOverall.<id>.success.status", "/v1/targets targets[].scoreStatus.<id>.status",
		"/v1/networks/{slug}/chart success[].status", "/v1/home market.mostReliable.status"},
		Values: codes(scoreRanked, "Scored and ranked among the networks of its kind.",
			notEnoughData, "Too few tries to score.",
			scoreTooFewNetworks, "Too few networks of the kind to rank.",
			scoreNoRankedTargets, "None of its targets is ranked.")})
	RegisterCodeSet(CodeSet{Name: "score_basis", Source: CodesFromAPI, UsedIn: []string{"* **.successScore.basis"},
		Values: codes(DayScoreBasis, "The network's success over every target, ranked among its kind.",
			MarketScoreBasis, "The median network's success rate.",
			FleetScoreBasis, "The mean success rate of the networks.",
			PassScoreBasis, "The success rate of the tests themselves.")})
	RegisterCodeSet(CodeSet{Name: "score_component", Source: CodesFromAPI,
		UsedIn: []string{"* **.breakdown.<key>", "/v1/meta weights.<key>", "/v1/meta profiles[].weights.<key>",
			"/v1/rankings weights.<key>", "/v1/rankings profiles[].weights.<key>"},
		Values: codes("reliability", "Success on the required targets.",
			"speed", "Latency under load.",
			"uptime", "Availability in the pulse tests.",
			"coverage", "Countries served.",
			"diversity", "Spread of the exit addresses over networks.",
			"dcAsns", "Few datacenter networks among the exits.",
			"authenticity", "Exits where the network says they are.",
			"value", "Price per GB.")})
	RegisterCodeSet(CodeSet{Name: "grade", Source: CodesFromAPI, UsedIn: []string{"* **.grade", "/v1/meta grades[].grade"},
		Values: []Code{grade("A+"), grade("A"), grade("B+"), grade("B"), grade("C+"), grade("C"), grade("D")}})
}

func init() {
	RegisterCodeSet(CodeSet{Name: "na_reason", Source: CodesFromAPI,
		UsedIn: append([]string{"* **.naReason", "* **.naReasons.<id>"}, coveragePaths("reason")...),
		Values: codes(NACatalog, "The catalog has no row for it.",
			NANotMeasured, "Not measured in production's cadence.",
			NANotApplicable, "It has no meaning for this row.",
			NAHistoryCoversRange, "The history covers the whole range.",
			NANotPublished, "Never published.",
			NANotMeasuredPerPlace, "Not measured per place.",
			NANoRun, "Measured, but no run in this window.",
			NAFewSuccesses, "Too few light-load successes for a latency.",
			NANoSuccesses, "Tries but no success.",
			NAIncompleteTargets, "A target of the bill has no cost.",
			NANoCheaperPeer, "No cheaper network of the kind with a success rate close enough.",
			NANoEvidence, "No monitoring evidence in the window.",
			NANoScoreToCompare, "No earlier score to compare with.",
			NANoPulseTests, "Too few finished pulse tests.",
			NANoLoadTest, "No load test in the window.",
			NARequiredTarget, "A required target has no ready result.",
			NANoPrice, "No verified, dated price per GB.",
			NAFewRequests, "Fewer requests than the methodology requires.",
			NALowCoverage, "Its requests cover too little of the window.",
			NAExpired, "Its measurements are too old.",
			NAInsufficient, "Not enough data for another stated reason.",
			notEnoughData, "A place's score: too few tries to score.",
			scoreTooFewNetworks, "A place's score: too few networks to rank.")})
}

// grade is a grade with the lowest score that earns it (/v1/meta grades[]).
func grade(g string) Code {
	return withParams(g, "Grade "+g+": a score of at least {min}.", param("min", "number", "score_0_100"))
}
