"""
PTP offer-eligibility gate tests -- pipeline/ptp_trigger.should_offer_ptp.

Plain-assert tests against the real local SQLite DB, same pattern
test_risk_tier.py/test_ptp_honor_break.py already use (unique uuid-suffixed
customer_ids per test so runs never collide with each other or with real
webhook traffic already in data/customer_history.db).

Run with:
    python tests/test_ptp_trigger.py
or via pytest:
    python -m pytest tests/test_ptp_trigger.py -v
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import customer_ptp_stats, guardrails, promise_store, ptp_trigger


def _new_customer_id(label: str) -> str:
    return f"cust_test_ptptrigger_{label}_{uuid.uuid4().hex[:8]}"


def _base_case(**overrides) -> dict:
    case = {
        "decline_code": "expired_card_soft",
        "retry_attempt_number": 1,
        "customer_id": _new_customer_id("base"),
        "ltv_tier": "medium",
        "payment_rail": "card",
    }
    case.update(overrides)
    return case


def _make_restricted_customer(label: str) -> str:
    """Drives a fresh customer_id to the 'restricted' risk tier via two
    consecutive broken promises -- same path test_risk_tier.py's
    _resolve_promise helper uses, since there's no direct 'set tier' writer
    (current_risk_tier is only ever derived by customer_ptp_stats.
    compute_risk_tier, never set directly -- see that module's docstring)."""
    customer_id = _new_customer_id(label)
    for _ in range(2):
        promise_id = promise_store.create_promise(
            f"case_ptptrigger_{uuid.uuid4().hex[:8]}", customer_id, "test reply"
        )
        promise_store.mark_promise_broken(promise_id)
        customer_ptp_stats.record_promise_resolution(customer_id, honored=False)
    assert customer_ptp_stats.get_risk_tier(customer_id) == customer_ptp_stats.RISK_TIER_RESTRICTED
    return customer_id


# --------------------------------------------------------------------------
# One test per trigger_category, per the brief.
# --------------------------------------------------------------------------
def test_restricted_tier_blocks_regardless_of_everything_else():
    customer_id = _make_restricted_customer("restricted")
    # Otherwise a clean "should offer" case (funds code, first failure) --
    # proves restricted_tier is checked FIRST and wins over every other rule.
    case = _base_case(customer_id=customer_id, decline_code="insufficient_funds", retry_attempt_number=1)

    result = ptp_trigger.should_offer_ptp(case)

    assert result["offer_ptp"] is False
    assert result["trigger_category"] == ptp_trigger.CATEGORY_RESTRICTED_TIER
    assert result["reason"]
    print("PASS  test_restricted_tier_blocks_regardless_of_everything_else")


def test_hard_decline_blocks_via_decline_code_mapper():
    # "card_number_invalid" is a real Razorpay error_reason in
    # decline_code_mapper._HARD_REASONS -- no decline_code_bucket is
    # precomputed on this bare case dict, so this exercises the
    # decline_code_mapper.map_razorpay_error_reason() fallback path
    # directly, not a precomputed field.
    case = _base_case(decline_code="card_number_invalid", retry_attempt_number=1)

    result = ptp_trigger.should_offer_ptp(case)

    assert result["offer_ptp"] is False
    assert result["trigger_category"] == ptp_trigger.CATEGORY_HARD_DECLINE
    assert result["reason"]
    print("PASS  test_hard_decline_blocks_via_decline_code_mapper")


def test_open_promise_exists_blocks_even_a_clean_true_case():
    customer_id = _new_customer_id("open_promise")
    promise_store.create_promise(f"case_ptptrigger_{uuid.uuid4().hex[:8]}", customer_id, "I'll pay soon")
    # Otherwise a clean "should offer" case -- proves open_promise_exists is
    # checked before the True triggers, not just before the other False ones.
    case = _base_case(customer_id=customer_id, decline_code="insufficient_funds", retry_attempt_number=1)

    result = ptp_trigger.should_offer_ptp(case)

    assert result["offer_ptp"] is False
    assert result["trigger_category"] == ptp_trigger.CATEGORY_OPEN_PROMISE_EXISTS
    assert result["reason"]
    print("PASS  test_open_promise_exists_blocks_even_a_clean_true_case")


def test_first_failure_awaiting_auto_retry_on_soft_decline():
    case = _base_case(decline_code="expired_card_soft", retry_attempt_number=1, ltv_tier="medium", payment_rail="card")

    result = ptp_trigger.should_offer_ptp(case)

    assert result["offer_ptp"] is False
    assert result["trigger_category"] == ptp_trigger.CATEGORY_FIRST_FAILURE_AWAITING_AUTO_RETRY
    assert result["reason"]
    print("PASS  test_first_failure_awaiting_auto_retry_on_soft_decline")


def test_high_ltv_first_failure_offers_even_on_attempt_one():
    case = _base_case(decline_code="expired_card_soft", retry_attempt_number=1, ltv_tier="high", payment_rail="card")

    result = ptp_trigger.should_offer_ptp(case)

    assert result["offer_ptp"] is True
    assert result["trigger_category"] == ptp_trigger.CATEGORY_HIGH_LTV_FIRST_FAILURE
    assert result["reason"]
    print("PASS  test_high_ltv_first_failure_offers_even_on_attempt_one")


def test_insufficient_funds_code_offers_regardless_of_retry_count():
    case = _base_case(decline_code="insufficient_funds", retry_attempt_number=1, ltv_tier="medium", payment_rail="card")

    result = ptp_trigger.should_offer_ptp(case)

    assert result["offer_ptp"] is True
    assert result["trigger_category"] == ptp_trigger.CATEGORY_INSUFFICIENT_FUNDS_CODE
    assert result["reason"]
    print("PASS  test_insufficient_funds_code_offers_regardless_of_retry_count")


def test_approaching_retry_cap_on_last_upi_attempt_before_npci_cap():
    # guardrails.NPCI_RETRY_CAP=4 -- the last real chance is attempt
    # NPCI_RETRY_CAP - 1 (the NEXT attempt would trip npci_retry_cap_reached).
    case = _base_case(
        decline_code="expired_card_soft",
        retry_attempt_number=guardrails.NPCI_RETRY_CAP - 1,
        ltv_tier="medium",
        payment_rail="upi_autopay",
    )

    result = ptp_trigger.should_offer_ptp(case)

    assert result["offer_ptp"] is True
    assert result["trigger_category"] == ptp_trigger.CATEGORY_APPROACHING_RETRY_CAP
    assert result["reason"]
    print("PASS  test_approaching_retry_cap_on_last_upi_attempt_before_npci_cap")


def test_retry_failed_once_offers_on_second_attempt():
    case = _base_case(decline_code="expired_card_soft", retry_attempt_number=2, ltv_tier="medium", payment_rail="card")

    result = ptp_trigger.should_offer_ptp(case)

    assert result["offer_ptp"] is True
    assert result["trigger_category"] == ptp_trigger.CATEGORY_RETRY_FAILED_ONCE
    assert result["reason"]
    print("PASS  test_retry_failed_once_offers_on_second_attempt")


def test_retry_failed_once_also_covers_attempts_past_the_second():
    # More failed attempts is never LESS of a reason to offer PTP -- attempt
    # 3 on a non-upi rail (not yet at the network cap) still offers.
    case = _base_case(decline_code="expired_card_soft", retry_attempt_number=3, ltv_tier="medium", payment_rail="card")

    result = ptp_trigger.should_offer_ptp(case)

    assert result["offer_ptp"] is True
    assert result["trigger_category"] == ptp_trigger.CATEGORY_RETRY_FAILED_ONCE
    print("PASS  test_retry_failed_once_also_covers_attempts_past_the_second")


_ALL_TESTS = [
    test_restricted_tier_blocks_regardless_of_everything_else,
    test_hard_decline_blocks_via_decline_code_mapper,
    test_open_promise_exists_blocks_even_a_clean_true_case,
    test_first_failure_awaiting_auto_retry_on_soft_decline,
    test_high_ltv_first_failure_offers_even_on_attempt_one,
    test_insufficient_funds_code_offers_regardless_of_retry_count,
    test_approaching_retry_cap_on_last_upi_attempt_before_npci_cap,
    test_retry_failed_once_offers_on_second_attempt,
    test_retry_failed_once_also_covers_attempts_past_the_second,
]


def main() -> int:
    failures = 0
    for test_fn in _ALL_TESTS:
        try:
            test_fn()
        except AssertionError as exc:
            print(f"FAIL  {test_fn.__name__}: {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001 -- surface a crash as a failure, not a traceback abort
            print(f"ERROR {test_fn.__name__}: {type(exc).__name__}: {exc}")
            failures += 1

    total = len(_ALL_TESTS)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
