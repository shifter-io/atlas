"""How build() scores a window: the cohort of a run, the slot length, the list prices, the
peer scores of one window of a peer group, their retention and their ranks."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from ..targets import registry
from .aggregate import BASIS_GB, PRICES, peer_scores
from .lists import CRITERIA
from .policy import digest

# Census and pulse runs (linked to a test batch) of a product share one cohort, whatever the
# country, target, node or concurrency: every provider runs them at the same moment, so they are
# comparable as a whole, and the breakdowns, uptime and census summary span all countries.
SCHEDULED = "census_pulse"
# The resolution of stored buckets: a time slot is a whole number of them (and of the pulse
# interval), so no stored request is ever placed in the wrong slot.
BUCKET_MINUTES = 5


def cohort_for(scope, kind, scheduled=False):
    if scheduled:
        criteria = dict.fromkeys(CRITERIA) | {"kind": kind, "schedule": SCHEDULED}
        return digest(criteria)[:20], criteria
    c = scope["config"]
    adapter = c.get("provider_config") or {}
    # Exclude provider implementation credentials/gateways; include all semantic parameters.
    # Custom URLs/version changes form distinct cohorts, never silently join the default target.
    target = scope["target"]
    known = registry.get(target["id"])
    variant = (
        None
        if (
            target["url"] == known.build_request().url
            and str(target["version"]) == str(known.version)
        )
        else target
    )
    criteria = {
        "kind": kind,
        "country": c.get("requested_country"),
        "runner_location": c.get("runner_location"),
        "concurrency": c["concurrency"],
        "timeout_seconds": c["timeout_seconds"],
        "rate": c.get("requests_per_second"),
        "proxy_protocol": c.get("proxy_protocol"),
        "session_policy": adapter.get("session_policy", "unspecified"),
        "parameters": adapter.get("parameters", {}),
        "flags": adapter.get("extra_flags", {}),
        "ttl_seconds": adapter.get("effective_ttl_seconds"),
        "target_variant": variant,
    }
    return digest(criteria)[:20], criteria


def retain(score, previous, as_of, valid_hours, observed, method_id=None):
    score["previous_rank"] = previous.get("rank") if previous else None
    score["methodology_id"] = method_id
    expires = (observed or as_of) + timedelta(hours=valid_hours)
    if score["score"] is not None and expires <= as_of:
        score.update(score=None, grade=None, state="expired", valid_until=expires)
        score.setdefault("reasons", []).append("Required measurement freshness deadline expired")
    if score["score"] is not None:
        # Validity follows the last measurement, not the time a calculation was rerun.
        score["scored_at"] = as_of
        score["valid_until"] = expires
    elif previous and previous["score"] is not None:
        score.update({k: previous[k] for k in ("score", "grade", "scored_at", "valid_until")})
        score["previous_components"] = previous.get("previous_components", previous["components"])
        score["state"] = (
            "retained" if datetime.fromisoformat(previous["valid_until"]) > as_of else "expired"
        )
    if previous and score["state"] in ("retained", "expired"):
        score["methodology_id"] = previous.get("methodology_id", method_id)
        if score["state"] == "retained" and score["methodology_id"] != method_id:
            score["state"] = "historical"
    return score


def slot_length(pulse_minutes):
    """Minutes in a time slot: the pulse interval, made a whole multiple of the 5-minute stored
    bucket (the least common multiple: 30 stays 30; staging's 2 becomes 10), so a slot always
    holds whole pulse intervals and whole buckets and a census request is never placed in a
    slot it did not run in. Without a pulse interval, the 5-minute bucket."""
    if not pulse_minutes:
        return BUCKET_MINUTES
    return math.lcm(max(1, round(pulse_minutes)), BUCKET_MINUTES)


def pricing(evidence_rows, product_id, cohort, as_of):
    """The product's list prices in effect at as_of (USD per GB of the pool that is tested).

    Prices are effective-dated metric_evidence (PRICES): per metric the newest record for the
    product (its cohort or "*") that started by as_of and is still valid. entry_per_gb is the
    smallest plan's rate, lowest_per_gb the lowest; basis_per_gb, the rate of the basis plan of
    basis_plan_gb GB, prices every cost, and price_basis says which plan that is: the first of
    200 GB or more ("first_200gb_plus") or the largest ("largest_below_200gb").
    Returns (the published pricing, the evidence rows it used).
    """
    chosen = {}
    for e in sorted(
        evidence_rows, key=lambda e: (e["period_start"], e["valid_until"], e.get("evidence_id", 0))
    ):
        if (
            e["metric"] in PRICES
            and e["product_id"] == product_id
            and e["cohort"] in (cohort, "*")
            and e["period_start"] <= as_of < e["valid_until"]
        ):
            chosen[e["metric"]] = e
    prices = {
        name: chosen[metric]["value"] if metric in chosen else None
        for metric, name in PRICES.items()
    }
    gb = prices["basis_plan_gb"]
    return {
        "currency": "USD",
        **prices,
        "price_basis": None
        if gb is None
        else "first_200gb_plus"
        if gb >= BASIS_GB
        else "largest_below_200gb",
        "effective_from": max((e["period_start"] for e in chosen.values()), default=None),
        "valid_until": min((e["valid_until"] for e in chosen.values()), default=None),
        "sources": sorted({e["source"] for e in chosen.values()}),
    }, [chosen[m] for m in PRICES if m in chosen]


def fresh(observed, as_of, valid_hours):
    """Whether measurements last observed then are recent enough for a new score (retain())."""
    return (observed or as_of) + timedelta(hours=valid_hours) > as_of


def score_window(members, label, config, as_of, method_id):
    """Score one window of a peer group by ratio to the leader, then retain and cap validity.

    members are (product, rows, observations, member) of one cohort; member holds the raw inputs
    peer_scores() needs and what retain() needs: the previous window ("old"), the evidence used
    and the observation times.
    """
    results = peer_scores([m for *_, m in members], config)
    for (p, *_, member), result in zip(members, results, strict=True):
        w = p["windows"][label]
        old = member["old"]
        for target, score in result["targets"].items():
            raw = member["targets"][target]
            retain(
                score,
                old.get("targets", {}).get(target, {}).get("score"),
                as_of,
                raw["valid_hours"],
                raw["observed"],
                method_id,
            )
            w["targets"][target]["score"] = score
        cfg = member["config"]
        for profile, score in result["scores"].items():
            retain(
                score,
                old.get("scores", {}).get(profile),
                as_of,
                cfg["score_valid_hours"],
                member["observed"],
                method_id,
            )
            if score["state"] == "ready" and member["evidence"]:
                score["valid_until"] = min(
                    score["valid_until"], *(e["valid_until"] for e in member["evidence"])
                )
            if score["state"] == "ready":
                target_expiries = [
                    w["targets"][t]["score"]["valid_until"] for t in cfg["required_targets"]
                ]
                score["valid_until"] = min(score["valid_until"], *target_expiries)
        w["scores"] = result["scores"]
        w["readiness"] = (
            "ready"
            if w["scores"]["balanced"]["state"] == "ready"
            else "partial"
            if w["metrics"]["completed"]
            else "unavailable"
        )


def rank(scores):
    eligible = sorted(
        (s for s in scores if s["state"] in ("ready", "retained")), key=lambda s: -s["score"]
    )
    # A single eligible product isn't a meaningful competitive ranking.
    if len(eligible) < 2:
        return
    for s in eligible:
        s["rank"] = 1 + sum(other["score"] > s["score"] for other in eligible)
