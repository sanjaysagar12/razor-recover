"""
Phase 13 tests -- PTP (Promise-to-Pay) guardrail extension.

Runs 10 sample customer replies through llm_layer.extract_promise_date
(Phase 10) followed by guardrails.apply_ptp_guardrails (Phase 13) and
asserts the expected guardrail outcome for each.

Extraction itself is exercised through a canned _MockDateAdapter rather than
a live provider call -- same reasoning as test_llm_layer.py's _MockAdapter:
this suite is testing guardrail behavior (deterministic date-math rules),
not Phase 10's NLP correctness, and a real model's exact date resolution
for something like "give me a week" isn't guaranteed stable across runs.
extract_promise_date's own validation/fallback contract (schema check,
retry-once, never-raise) still runs for real -- only the network call is
stubbed.

Run with:
    python pipeline/test_ptp_guardrails.py
or via pytest:
    python -m pytest pipeline/test_ptp_guardrails.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import guardrails
import llm_layer

# Fixed "today" so every date-math assertion below (30-day cap, past-date,
# +45-days, etc.) is deterministic regardless of when this suite runs.
TODAY = "2026-08-31"


class _MockDateAdapter(llm_layer.LLMAdapter):
    """Stand-in for a real provider's extract_date() -- returns a canned,
    schema-shaped extraction per message text instead of calling an LLM."""

    def __init__(self, canned: dict):
        self._canned = canned

    def generate(self, case: dict) -> dict:
        raise NotImplementedError("this mock is for extract_date only")

    def extract_date(self, message: str, case_context: dict) -> dict:
        return {**llm_layer._echoed_date_fields("mock:test-model"), **self._canned[message]}


def _case(**overrides) -> dict:
    case = {
        "case_id": "case_ptp_0001",
        "today": TODAY,
        "payment_rail": "card",
        "amount": 499.0,
        "decline_code": "issuer_unavailable",
    }
    case.update(overrides)
    return case


def _extract(message: str, canned: dict, case_context: dict) -> dict:
    adapter = _MockDateAdapter({message: canned})
    return llm_layer.extract_promise_date(message, case_context, adapter=adapter)


def _report(result: dict) -> None:
    """Prints the calling test's name plus one result's key fields -- called
    at the end of every test function, after its asserts pass, so `main()`
    below shows the guardrail verdict for all 10 cases without each test
    needing to `return` a value (pytest warns on non-None test returns)."""
    name = sys._getframe(1).f_code.co_name
    print(f"PASS  {name}")
    print(f"      guardrail_status={result['guardrail_status']!r} "
          f"final_action={result['final_action']!r} "
          f"guardrail_flags={result['guardrail_flags']!r} "
          f"requires_human_review={result['requires_human_review']!r} "
          f"routed_to_clarification={result['routed_to_clarification']!r}")


# --------------------------------------------------------------------------
# 1. Specific near-term date, within window -> approved
# --------------------------------------------------------------------------
def test_1_specific_date_within_window_approved():
    message = "I'll pay on the 5th"
    case = _case()
    extraction = _extract(message, {
        "extracted_date": "2026-09-05",
        "confidence": 0.9,
        "ambiguous": False,
        "clarification_needed": None,
    }, case)

    result = guardrails.apply_ptp_guardrails(case, extraction)
    assert result["guardrail_status"] == "approved"
    assert result["guardrail_flags"] == []
    assert result["final_action"] == guardrails.PTP_PROCEED_ACTION
    assert result["requires_human_review"] is False
    _report(result)


# --------------------------------------------------------------------------
# 2. Vague-but-resolvable relative date -> approved (unambiguous extraction)
# --------------------------------------------------------------------------
def test_2_give_me_a_week_approved():
    message = "give me a week"
    case = _case()
    extraction = _extract(message, {
        "extracted_date": "2026-09-07",
        "confidence": 0.65,
        "ambiguous": False,
        "clarification_needed": None,
    }, case)

    result = guardrails.apply_ptp_guardrails(case, extraction)
    assert result["guardrail_status"] == "approved"
    assert result["final_action"] == guardrails.PTP_PROCEED_ACTION
    _report(result)


# --------------------------------------------------------------------------
# 3. No extractable date -> pending_clarification
# --------------------------------------------------------------------------
def test_3_no_commitment_pending_clarification():
    message = "I'm broke right now"
    case = _case()
    extraction = _extract(message, {
        "extracted_date": None,
        "confidence": 0.05,
        "ambiguous": True,
        "clarification_needed": "Could you give me a specific date you'd like to pay by?",
    }, case)

    result = guardrails.apply_ptp_guardrails(case, extraction)
    assert result["guardrail_status"] == "pending_clarification"
    assert result["guardrail_flags"] == ["low_confidence_gate"]
    assert result["final_action"] == guardrails.PTP_PENDING_CLARIFICATION_ACTION
    assert result["routed_to_clarification"] is True
    assert result["requires_human_review"] is False
    _report(result)


# --------------------------------------------------------------------------
# 4. Too ambiguous to resolve to one day -> pending_clarification
# --------------------------------------------------------------------------
def test_4_too_ambiguous_pending_clarification():
    message = "maybe next month sometime"
    case = _case()
    extraction = _extract(message, {
        "extracted_date": None,
        "confidence": 0.1,
        "ambiguous": True,
        "clarification_needed": "Could you give me a specific date you'd like to pay by?",
    }, case)

    result = guardrails.apply_ptp_guardrails(case, extraction)
    assert result["guardrail_status"] == "pending_clarification"
    assert result["guardrail_flags"] == ["low_confidence_gate"]
    _report(result)


# --------------------------------------------------------------------------
# 5. Resolves 45+ days out -> rejected, window_cap
# --------------------------------------------------------------------------
def test_5_far_future_date_rejected_window_cap():
    message = "I'll definitely pay in 45 days, promise"
    case = _case()
    extraction = _extract(message, {
        "extracted_date": "2026-10-15",  # 45 days after TODAY
        "confidence": 0.85,
        "ambiguous": False,
        "clarification_needed": None,
    }, case)

    result = guardrails.apply_ptp_guardrails(case, extraction)
    assert result["guardrail_status"] == "rejected_window_cap"
    assert result["guardrail_flags"] == ["window_cap_check"]
    assert result["final_action"] == guardrails.NO_RETRY_ACTION
    assert result["requires_human_review"] is True
    assert result["routed_to_clarification"] is False
    _report(result)


# --------------------------------------------------------------------------
# 6. Resolves to yesterday -> rejected, past_date, routed to clarification
# --------------------------------------------------------------------------
def test_6_past_date_rejected_routed_to_clarification():
    message = "I already sent it on the 30th"
    case = _case()
    extraction = _extract(message, {
        "extracted_date": "2026-08-30",  # one day before TODAY
        "confidence": 0.8,
        "ambiguous": False,
        "clarification_needed": None,
    }, case)

    result = guardrails.apply_ptp_guardrails(case, extraction)
    assert result["guardrail_status"] == "rejected_past_date"
    assert result["guardrail_flags"] == ["past_date_check"]
    assert result["final_action"] == guardrails.NO_RETRY_ACTION
    assert result["requires_human_review"] is False
    assert result["routed_to_clarification"] is True
    _report(result)


# --------------------------------------------------------------------------
# 7. UPI Autopay, extracted execution time 11am IST -> adjusted, not rejected
# --------------------------------------------------------------------------
def test_7_upi_autopay_peak_window_adjusted_not_rejected():
    message = "I'll pay via autopay on the 3rd"
    case = _case(payment_rail="upi_autopay")
    extraction = _extract(message, {
        "extracted_date": "2026-09-03",
        "confidence": 0.85,
        "ambiguous": False,
        "clarification_needed": None,
    }, case)
    # schema.PromiseDateExtraction only carries a date, not a time (Phase 10
    # doesn't extract one) -- action_scheduled_for is silently dropped by
    # validate_promise_date_output's schema validation if passed through
    # _extract above, since it's not a declared field. A real scheduling
    # step (not built yet) would derive an execution timestamp from the
    # promised date and attach it here before guardrails run; simulate that
    # attachment directly for this test.
    # 05:30 UTC == 11:00 IST -- inside the [10:00, 13:00) IST peak window.
    extraction["action_scheduled_for"] = "2026-09-03T05:30:00Z"

    result = guardrails.apply_ptp_guardrails(case, extraction)
    assert result["guardrail_status"] == "adjusted"
    assert result["guardrail_flags"] == ["npci_peak_window_check"]
    assert result["final_action"] == guardrails.PTP_PROCEED_ACTION  # not rejected
    assert result["original_extracted_date"] == "2026-09-03T05:30:00Z"
    assert result["adjusted_date"] is not None
    assert result["adjusted_date"] != result["original_extracted_date"]
    # Prove the adjusted timestamp actually lands outside the peak window.
    adjusted_hour = guardrails._scheduled_hour_ist(result["adjusted_date"])
    assert not (guardrails.NPCI_PEAK_WINDOW_START_HOUR <= adjusted_hour < guardrails.NPCI_PEAK_WINDOW_END_HOUR)
    _report(result)


# --------------------------------------------------------------------------
# 8. Clearly specified date, within window -> approved
# --------------------------------------------------------------------------
def test_8_clearly_specified_date_within_window_approved():
    message = "I will pay on September 10th"
    case = _case()
    extraction = _extract(message, {
        "extracted_date": "2026-09-10",
        "confidence": 0.95,
        "ambiguous": False,
        "clarification_needed": None,
    }, case)

    result = guardrails.apply_ptp_guardrails(case, extraction)
    assert result["guardrail_status"] == "approved"
    assert result["guardrail_flags"] == []
    _report(result)


# --------------------------------------------------------------------------
# 9. Empty/garbage message -> pending_clarification, not a crash
# --------------------------------------------------------------------------
def test_9_empty_message_pending_clarification_no_crash():
    message = ""
    case = _case()
    extraction = _extract(message, {
        "extracted_date": None,
        "confidence": 0.0,
        "ambiguous": True,
        "clarification_needed": "Could you give me a specific date you'd like to pay by?",
    }, case)

    result = guardrails.apply_ptp_guardrails(case, extraction)
    assert result["guardrail_status"] == "pending_clarification"
    assert result["final_action"] == guardrails.PTP_PENDING_CLARIFICATION_ACTION
    _report(result)


# --------------------------------------------------------------------------
# 10. pattern_conflict_check -- SKIPPED.
#
# No per-customer day-of-month/time-of-day success-pattern store exists
# anywhere in this codebase (pipeline/customer_history.py tracks tenure,
# ltv_tier, historical_ptp_honor_rate as an overall rate, and
# prior_retry_success_count -- none of that is a day/time breakdown). Per
# the Phase 13 brief, pattern_conflict_check is skipped rather than invented
# against fabricated state, so there is no test 10 / no "flagged_conflict"
# guardrail_status to exercise. If a historical day/time pattern source is
# built later, add pattern_conflict_check to PTP_GUARDRAIL_RULES in
# guardrails.py and a test 10 here alongside it.
# --------------------------------------------------------------------------


_ALL_TESTS = [
    test_1_specific_date_within_window_approved,
    test_2_give_me_a_week_approved,
    test_3_no_commitment_pending_clarification,
    test_4_too_ambiguous_pending_clarification,
    test_5_far_future_date_rejected_window_cap,
    test_6_past_date_rejected_routed_to_clarification,
    test_7_upi_autopay_peak_window_adjusted_not_rejected,
    test_8_clearly_specified_date_within_window_approved,
    test_9_empty_message_pending_clarification_no_crash,
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

    print(
        "SKIP  test_10_pattern_conflict -- no per-customer day-of-month/time-of-day "
        "success-pattern store exists (see comment above); pattern_conflict_check not implemented."
    )

    total = len(_ALL_TESTS)
    print(f"\n{total - failures}/{total} passed (1 skipped: pattern_conflict_check has no data source)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
