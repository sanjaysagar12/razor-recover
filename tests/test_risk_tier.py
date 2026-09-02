"""
Phase 17 tests -- feedback loop / escalation ladder (current_risk_tier).

Exercises:
  * pipeline/customer_ptp_stats.compute_risk_tier -- the pure transition
    function, directly (no DB, no pipeline).
  * pipeline/customer_ptp_stats.record_promise_resolution -- the real
    upsert path (same call order pipeline/ptp_outcomes.py uses: mark the
    promise honored/broken first, then record the resolution), against the
    local SQLite DB, same pattern test_ptp_honor_break.py already uses.
  * pipeline/guardrails.apply_guardrails' new customer_risk_restricted rule.
  * pipeline/confidence_gate.route_case's new risk_tier parameter.

No real Razorpay API calls, no LLM calls -- entirely deterministic, per the
Phase 17 brief.

Run with:
    python pipeline/test_risk_tier.py
or via pytest:
    python -m pytest pipeline/test_risk_tier.py -v
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import confidence_gate
import customer_ptp_stats
import guardrails
import promise_store


def _new_customer_id(label: str) -> str:
    return f"cust_test_risk_{label}_{uuid.uuid4().hex[:8]}"


def _resolve_promise(customer_id: str, honored: bool) -> dict:
    """One promise, created and immediately resolved -- same order
    pipeline/ptp_outcomes.py's handlers use (promise_store.mark_promise_*
    BEFORE customer_ptp_stats.record_promise_resolution), so
    promise_store.get_recent_resolved_outcomes sees this resolution's own
    outcome by the time record_promise_resolution reads it back."""
    promise_id = promise_store.create_promise(
        f"case_risk_{uuid.uuid4().hex[:8]}", customer_id, "test reply"
    )
    if honored:
        promise_store.mark_promise_honored(promise_id)
    else:
        promise_store.mark_promise_broken(promise_id)
    return customer_ptp_stats.record_promise_resolution(customer_id, honored=honored)


def _base_case(**overrides) -> dict:
    case = {
        "case_id": "case_x",
        "decline_code": "issuer_unavailable",
        "payment_rail": "card",
        "retry_attempt_number": 1,
        "cumulative_retries_this_txn": 1,
    }
    case.update(overrides)
    return case


def _base_proposed(**overrides) -> dict:
    proposed = {
        "case_id": "case_x",
        "tree_model_score": 0.5,
        "tree_model_top_features": [],
        "recommended_action": "retry_now",
        "action_scheduled_for": None,
        "confidence": 0.8,
        "reasoning_summary": "test",
        "guardrail_flags": [],
        "requires_human_review": False,
        "model_version": "test",
        "timestamp": "2026-08-31T00:00:00+00:00",
    }
    proposed.update(overrides)
    return proposed


# --------------------------------------------------------------------------
# Pure-function unit tests -- compute_risk_tier, no DB involved.
# --------------------------------------------------------------------------
def test_pure_normal_stays_normal_when_rate_ok():
    tier = customer_ptp_stats.compute_risk_tier(
        current_tier="normal", promises_made=5, historical_ptp_honor_rate=0.6,
        recent_outcomes=["honored", "broken"],
    )
    assert tier == "normal"
    print("PASS  test_pure_normal_stays_normal_when_rate_ok")


def test_pure_normal_to_watch_when_rate_drops_without_consecutive_broken():
    tier = customer_ptp_stats.compute_risk_tier(
        current_tier="normal", promises_made=3, historical_ptp_honor_rate=0.333,
        recent_outcomes=["broken", "honored"],
    )
    assert tier == "watch"
    print("PASS  test_pure_normal_to_watch_when_rate_drops_without_consecutive_broken")


def test_pure_normal_to_restricted_when_two_consecutive_broken_coincide_with_rate_drop():
    # promises_made=2 with rate<0.5 is only reachable if both broke -- see
    # compute_risk_tier's docstring on why this lands directly on
    # 'restricted' rather than stopping at 'watch'.
    tier = customer_ptp_stats.compute_risk_tier(
        current_tier="normal", promises_made=2, historical_ptp_honor_rate=0.0,
        recent_outcomes=["broken", "broken"],
    )
    assert tier == "restricted"
    print("PASS  test_pure_normal_to_restricted_when_two_consecutive_broken_coincide_with_rate_drop")


def test_pure_watch_to_restricted_on_second_consecutive_broken():
    tier = customer_ptp_stats.compute_risk_tier(
        current_tier="watch", promises_made=6, historical_ptp_honor_rate=0.4,
        recent_outcomes=["broken", "broken"],
    )
    assert tier == "restricted"
    print("PASS  test_pure_watch_to_restricted_on_second_consecutive_broken")


def test_pure_watch_recovers_to_normal_on_honored():
    tier = customer_ptp_stats.compute_risk_tier(
        current_tier="watch", promises_made=6, historical_ptp_honor_rate=0.5,
        recent_outcomes=["honored", "broken"],
    )
    assert tier == "normal"
    print("PASS  test_pure_watch_recovers_to_normal_on_honored")


def test_pure_restricted_never_auto_clears():
    # Not even a run of honored promises resets it -- requires explicit
    # human review per the Phase 17 brief (see
    # customer_ptp_stats.TODO_RESTRICTED_RESET_REQUIRES_HUMAN_REVIEW).
    tier = customer_ptp_stats.compute_risk_tier(
        current_tier="restricted", promises_made=10, historical_ptp_honor_rate=1.0,
        recent_outcomes=["honored", "honored"],
    )
    assert tier == "restricted"
    print("PASS  test_pure_restricted_never_auto_clears")


# --------------------------------------------------------------------------
# Exit criterion 1 -- 2 consecutive broken promises -> restricted -> 3rd
# failed-payment case routes straight to requires_human_review, without
# going through the chat/date-extraction flow (this test never calls
# llm_layer.extract_promise_date or guardrails.apply_ptp_guardrails at all --
# the routing decision below comes entirely from apply_guardrails, proving
# the bypass structurally rather than by absence of a mock call).
# --------------------------------------------------------------------------
def test_1_two_consecutive_broken_reaches_restricted_and_bypasses_to_human_review():
    customer_id = _new_customer_id("restricted")

    _resolve_promise(customer_id, honored=False)  # 1st broken: promises_made=1, stays 'normal'
    result_2 = _resolve_promise(customer_id, honored=False)  # 2nd consecutive broken
    assert result_2["current_risk_tier"] == "restricted", (
        f"expected restricted after 2 consecutive broken promises, got {result_2['current_risk_tier']!r}"
    )

    stats = customer_ptp_stats.get_stats(customer_id)
    assert stats["current_risk_tier"] == "restricted"
    assert customer_ptp_stats.get_risk_tier(customer_id) == "restricted"

    # 3rd failed-payment case for this now-restricted customer.
    case = _base_case(current_risk_tier=customer_ptp_stats.get_risk_tier(customer_id))
    proposed = _base_proposed(recommended_action="retry_now", confidence=0.9)
    guardrail_result = guardrails.apply_guardrails(case, proposed)

    assert guardrail_result["final_action"] == "escalate_human", guardrail_result["final_action"]
    assert guardrail_result["requires_human_review"] is True
    assert "customer_risk_restricted" in guardrail_result["guardrail_flags"]

    print("PASS  test_1_two_consecutive_broken_reaches_restricted_and_bypasses_to_human_review")


# --------------------------------------------------------------------------
# Exit criterion 2 -- honor rate drops below 0.5 (without 2 consecutive
# broken, so the customer lands on 'watch', not 'restricted') -> next case
# routes to the LLM layer even with a tree-score outside the normal
# ambiguous band.
# --------------------------------------------------------------------------
def test_2_watch_tier_from_rate_drop_routes_to_llm_outside_probability_band():
    customer_id = _new_customer_id("watch")

    _resolve_promise(customer_id, honored=False)  # made=1, rate=0.0 -- promises_made<2, stays 'normal'
    _resolve_promise(customer_id, honored=True)   # made=2, rate=0.5 -- not <0.5, stays 'normal'
    result_3 = _resolve_promise(customer_id, honored=False)  # made=3, rate=0.333<0.5; recent=[broken,honored]

    assert result_3["current_risk_tier"] == "watch", (
        f"expected watch tier, got {result_3['current_risk_tier']!r} "
        f"(recent outcomes were NOT 2 consecutive broken -- see test setup)"
    )

    band_low, band_high = 0.4, 0.6
    tree_probability = 0.9  # well outside the band on its own
    decline_code = "insufficient_funds"  # not in AMBIGUOUS_DECLINE_CODES

    without_risk_tier = confidence_gate.route_case(
        "case_watch_no_tier", tree_probability, decline_code, band_low, band_high, is_ambiguous=False,
    )
    assert without_risk_tier["routed_to_llm"] is False, (
        "sanity check: this score/decline_code combo must NOT route to the LLM on its own, "
        "otherwise this test wouldn't prove the watch tier is what tips it over"
    )

    with_watch_tier = confidence_gate.route_case(
        "case_watch_3", tree_probability, decline_code, band_low, band_high,
        is_ambiguous=False, risk_tier=customer_ptp_stats.get_risk_tier(customer_id),
    )
    assert with_watch_tier["routed_to_llm"] is True
    assert "watch_tier_customer" in with_watch_tier["routing_trigger"]
    assert with_watch_tier["template_action"] is None

    print("PASS  test_2_watch_tier_from_rate_drop_routes_to_llm_outside_probability_band")


# --------------------------------------------------------------------------
# Exit criterion 3 -- a watch-tier customer whose next promise IS honored
# returns to 'normal'.
# --------------------------------------------------------------------------
def test_3_watch_tier_recovers_to_normal_on_next_honored_promise():
    customer_id = _new_customer_id("recovery")

    _resolve_promise(customer_id, honored=False)  # made=1, stays 'normal'
    _resolve_promise(customer_id, honored=True)   # made=2, rate=0.5, stays 'normal'
    watch_result = _resolve_promise(customer_id, honored=False)  # made=3, rate=0.333<0.5 -> watch
    assert watch_result["current_risk_tier"] == "watch"

    recovered_result = _resolve_promise(customer_id, honored=True)  # next promise honored
    assert recovered_result["current_risk_tier"] == "normal", (
        f"expected recovery to normal after an honored promise from watch tier, "
        f"got {recovered_result['current_risk_tier']!r}"
    )
    assert customer_ptp_stats.get_risk_tier(customer_id) == "normal"

    print("PASS  test_3_watch_tier_recovers_to_normal_on_next_honored_promise")


# --------------------------------------------------------------------------
# Exit criterion 4 -- a normal-tier customer's routing behavior is provably
# unchanged versus before Phase 17: same confidence_gate.route_case inputs
# with no risk_tier argument at all (the pre-Phase-17 call signature) versus
# risk_tier="normal" produce identical routing, and apply_guardrails never
# adds customer_risk_restricted / touches final_action for a normal-tier (or
# risk-tier-absent) case -- same "clean case, no rules fire" shape
# test_guardrails.py's own regression test already asserts.
# --------------------------------------------------------------------------
def test_4_normal_tier_routing_unchanged_from_pre_phase17_behavior():
    band_low, band_high = 0.4, 0.6

    pre_phase17_call = confidence_gate.route_case(
        "case_normal_1", 0.9, "insufficient_funds", band_low, band_high, is_ambiguous=False,
    )
    explicit_normal_call = confidence_gate.route_case(
        "case_normal_1", 0.9, "insufficient_funds", band_low, band_high, is_ambiguous=False, risk_tier="normal",
    )
    assert pre_phase17_call == explicit_normal_call

    in_band_call_pre = confidence_gate.route_case(
        "case_normal_2", 0.5, "insufficient_funds", band_low, band_high, is_ambiguous=False,
    )
    in_band_call_normal = confidence_gate.route_case(
        "case_normal_2", 0.5, "insufficient_funds", band_low, band_high, is_ambiguous=False, risk_tier="normal",
    )
    assert in_band_call_pre == in_band_call_normal
    assert in_band_call_pre["routed_to_llm"] is True  # probability_band trigger, unrelated to risk tier

    case = _base_case(current_risk_tier="normal")
    proposed = _base_proposed()
    result = guardrails.apply_guardrails(case, proposed)
    assert result["guardrail_flags"] == []
    assert result["final_action"] == result["proposed_action"] == "retry_now"
    assert result["requires_human_review"] is False

    # Same again with current_risk_tier entirely absent from the case dict
    # (e.g. a caller that predates Phase 17) -- must behave identically.
    case_no_tier_field = _base_case()
    result_no_tier_field = guardrails.apply_guardrails(case_no_tier_field, proposed)
    assert result_no_tier_field["guardrail_flags"] == []
    assert result_no_tier_field["final_action"] == "retry_now"

    print("PASS  test_4_normal_tier_routing_unchanged_from_pre_phase17_behavior")


# --------------------------------------------------------------------------
# Interaction test (added per review) -- a restricted-tier customer's
# hard-decline case must still show hard_decline_excluded in guardrail_flags
# (the underlying classification is never erased from the audit trail), but
# final_action is forced to escalate_human, not hard_decline_excluded's own
# no_retry_prompt_update -- proving customer_risk_restricted's override wins
# without hiding the fact that the case was ALSO a hard decline.
# --------------------------------------------------------------------------
def test_5_restricted_customer_hard_decline_keeps_both_flags_but_escalates():
    case = _base_case(decline_code="stolen_card", current_risk_tier="restricted")
    proposed = _base_proposed(recommended_action="retry_now")
    result = guardrails.apply_guardrails(case, proposed)

    assert "customer_risk_restricted" in result["guardrail_flags"]
    assert "hard_decline_excluded" in result["guardrail_flags"]
    assert result["final_action"] == "escalate_human", (
        f"customer_risk_restricted must win final_action over hard_decline_excluded, "
        f"got final_action={result['final_action']!r}"
    )
    assert result["final_action"] != "no_retry_prompt_update"
    assert result["requires_human_review"] is True
    assert result["proposed_action"] == "retry_now"  # never overwritten, same as every other override

    print("PASS  test_5_restricted_customer_hard_decline_keeps_both_flags_but_escalates")


_ALL_TESTS = [
    test_pure_normal_stays_normal_when_rate_ok,
    test_pure_normal_to_watch_when_rate_drops_without_consecutive_broken,
    test_pure_normal_to_restricted_when_two_consecutive_broken_coincide_with_rate_drop,
    test_pure_watch_to_restricted_on_second_consecutive_broken,
    test_pure_watch_recovers_to_normal_on_honored,
    test_pure_restricted_never_auto_clears,
    test_1_two_consecutive_broken_reaches_restricted_and_bypasses_to_human_review,
    test_2_watch_tier_from_rate_drop_routes_to_llm_outside_probability_band,
    test_3_watch_tier_recovers_to_normal_on_next_honored_promise,
    test_4_normal_tier_routing_unchanged_from_pre_phase17_behavior,
    test_5_restricted_customer_hard_decline_keeps_both_flags_but_escalates,
]


def main() -> int:
    failures = 0
    for test_fn in _ALL_TESTS:
        try:
            test_fn()
        except AssertionError as exc:
            print(f"FAIL  {test_fn.__name__}: {exc}")
            failures += 1
            continue
        except Exception as exc:  # noqa: BLE001 -- surface a crash as a failure, not a traceback abort
            print(f"ERROR {test_fn.__name__}: {type(exc).__name__}: {exc}")
            failures += 1

    total = len(_ALL_TESTS)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
