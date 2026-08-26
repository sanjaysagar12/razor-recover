"""
Phase 6 tests -- Guardrail layer.

Plain-assert tests, runnable either directly:
    python pipeline/test_guardrails.py
or via pytest:
    python -m pytest pipeline/test_guardrails.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import guardrails


def _base_case(**overrides) -> dict:
    case = {
        "case_id": "case_x",
        "decline_code": "issuer_unavailable",
        "payment_rail": "card",
        "retry_attempt_number": 1,
        "cumulative_retries_this_txn": 2,
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
        "timestamp": "2026-08-26T00:00:00+00:00",
    }
    proposed.update(overrides)
    return proposed


def test_clean_case_no_rules_fire():
    result = guardrails.apply_guardrails(_base_case(), _base_proposed())
    assert result["guardrail_flags"] == []
    assert result["final_action"] == result["proposed_action"] == "retry_now"
    assert result["requires_human_review"] is False


def test_hard_decline_excluded_fires():
    # Real decline_code (data/generate_synthetic.py's CLEAR_HARD bucket), not
    # a placeholder string -- regression guard for the HARD_DECLINE_CODES
    # mismatch that made this rule 100% dead on real batch data.
    case = _base_case(decline_code="stolen_card")
    proposed = _base_proposed(recommended_action="retry_now")
    result = guardrails.apply_guardrails(case, proposed)
    assert result["guardrail_flags"] == ["hard_decline_excluded"]
    assert result["final_action"] == "no_retry_prompt_update"
    assert result["proposed_action"] == "retry_now"  # never overwritten


def test_npci_retry_cap_reached_fires():
    case = _base_case(payment_rail="upi_autopay", retry_attempt_number=4)
    proposed = _base_proposed(recommended_action="retry_scheduled")
    result = guardrails.apply_guardrails(case, proposed)
    assert "npci_retry_cap_reached" in result["guardrail_flags"]
    assert "npci_peak_window" not in result["guardrail_flags"]
    assert result["final_action"] == "no_retry_prompt_update"


def test_npci_peak_window_fires():
    # 06:00 UTC = 11:30 IST -- inside the 10:00-13:00 IST window. Timestamp
    # is UTC (not IST) because that's what action_scheduled_for is actually
    # emitted as (see _scheduled_hour_ist) -- the window check converts.
    case = _base_case(payment_rail="upi_autopay", retry_attempt_number=1)
    proposed = _base_proposed(
        recommended_action="retry_scheduled",
        action_scheduled_for="2026-08-27T06:00:00+00:00",
    )
    result = guardrails.apply_guardrails(case, proposed)
    assert "npci_peak_window" in result["guardrail_flags"]
    assert "npci_retry_cap_reached" not in result["guardrail_flags"]
    assert result["final_action"] == "no_retry_prompt_update"


def test_network_retry_cap_exceeded_fires():
    # cumulative_retries_this_txn=5 is the real max observed for
    # retry_attempt_number in data/train.csv + data/holdout.csv -- proves the
    # rule fires at a value actually reachable by real data, not just a
    # synthetic value against a field that never existed upstream.
    case = _base_case(cumulative_retries_this_txn=5)
    proposed = _base_proposed(recommended_action="retry_now")
    result = guardrails.apply_guardrails(case, proposed)
    assert result["guardrail_flags"] == ["network_retry_cap_exceeded"]
    assert result["final_action"] == "no_retry_prompt_update"


def test_off_enum_action_flips_human_review_without_changing_final_action():
    case = _base_case()
    proposed = _base_proposed(recommended_action="cancel_subscription", confidence=0.9)
    result = guardrails.apply_guardrails(case, proposed)
    assert result["guardrail_flags"] == ["requires_human_review_floor"]
    assert result["requires_human_review"] is True
    assert result["final_action"] == result["proposed_action"] == "cancel_subscription"


def test_low_confidence_flips_human_review_without_changing_final_action():
    case = _base_case()
    proposed = _base_proposed(recommended_action="retry_now", confidence=0.2)
    result = guardrails.apply_guardrails(case, proposed)
    assert result["guardrail_flags"] == ["requires_human_review_floor"]
    assert result["requires_human_review"] is True
    assert result["final_action"] == "retry_now"


def test_multiple_rules_fire_first_override_wins_on_final_action():
    # hard_decline_excluded fires first in rule order, so its override wins
    # final_action even though network_retry_cap_exceeded also fires.
    case = _base_case(decline_code="stolen_card", cumulative_retries_this_txn=20)
    proposed = _base_proposed(recommended_action="retry_now")
    result = guardrails.apply_guardrails(case, proposed)
    assert result["guardrail_flags"] == ["hard_decline_excluded", "network_retry_cap_exceeded"]
    assert result["final_action"] == "no_retry_prompt_update"


def test_run_guardrails_on_batch_applies_across_list():
    cases = [_base_case(), _base_case(decline_code="stolen_card")]
    proposed_list = [_base_proposed(), _base_proposed()]
    results = guardrails.run_guardrails_on_batch(cases, proposed_list)
    assert len(results) == 2
    assert results[0]["guardrail_flags"] == []
    assert results[1]["guardrail_flags"] == ["hard_decline_excluded"]


def test_proposed_guardrail_flags_not_carried_forward():
    # proposed.guardrail_flags may carry an upstream label Phase 6 never
    # verified (e.g. the LLM echoing a case_facts.guardrail_flags value).
    # Regression guard for a bug where seeding result["guardrail_flags"]
    # from proposed produced a duplicate whenever that echoed label happened
    # to share a name with a rule that also fired here for real.
    case = _base_case(payment_rail="upi_autopay", retry_attempt_number=4)
    proposed = _base_proposed(
        recommended_action="retry_scheduled",
        guardrail_flags=["npci_retry_cap_reached", "some_other_llm_echoed_label"],
    )
    result = guardrails.apply_guardrails(case, proposed)
    assert result["guardrail_flags"] == ["npci_retry_cap_reached"]  # exactly once
    assert "some_other_llm_echoed_label" not in result["guardrail_flags"]


def test_npci_peak_window_converts_utc_to_ist_before_checking():
    # Real failing example from the batch wiring check: 10:30 UTC is 16:00
    # IST -- outside the 10:00-13:00 IST peak window -- so this must NOT
    # fire even though the raw "10" hour digit looks like a match.
    case = _base_case(payment_rail="upi_autopay")
    proposed = _base_proposed(
        recommended_action="retry_scheduled",
        action_scheduled_for="2023-10-27T10:30:00Z",
    )
    result = guardrails.apply_guardrails(case, proposed)
    assert "npci_peak_window" not in result["guardrail_flags"]

    # 05:30 UTC is 11:00 IST -- inside the window -- must fire.
    proposed2 = _base_proposed(
        recommended_action="retry_scheduled",
        action_scheduled_for="2023-10-27T05:30:00Z",
    )
    result2 = guardrails.apply_guardrails(case, proposed2)
    assert "npci_peak_window" in result2["guardrail_flags"]


_ALL_TESTS = [
    test_clean_case_no_rules_fire,
    test_hard_decline_excluded_fires,
    test_npci_retry_cap_reached_fires,
    test_npci_peak_window_fires,
    test_network_retry_cap_exceeded_fires,
    test_off_enum_action_flips_human_review_without_changing_final_action,
    test_low_confidence_flips_human_review_without_changing_final_action,
    test_multiple_rules_fire_first_override_wins_on_final_action,
    test_run_guardrails_on_batch_applies_across_list,
    test_proposed_guardrail_flags_not_carried_forward,
    test_npci_peak_window_converts_utc_to_ist_before_checking,
]


def main() -> int:
    failures = 0
    for test_fn in _ALL_TESTS:
        try:
            test_fn()
        except AssertionError as exc:
            print(f"FAIL  {test_fn.__name__}: {exc}")
            failures += 1
        else:
            print(f"PASS  {test_fn.__name__}")

    total = len(_ALL_TESTS)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
