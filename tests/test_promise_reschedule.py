"""
Phase 14 tests -- PTP (Promise-to-Pay) reschedule execution.

Exercises pipeline/execute_action.execute_promise_reschedule and
pipeline/run_case.run_promise_reschedule against a real guardrails.
apply_ptp_guardrails() verdict (same fixed TODAY convention as
test_ptp_guardrails.py, so date-math assertions are deterministic).

Test 1 makes a REAL call against the Razorpay TEST-MODE account configured in
.env (razorpay_client.get_client() refuses to run against anything but a
rzp_test_ key -- see pipeline/razorpay_client.py) -- it needs network access
and valid test-mode credentials to pass. Tests 2/3 monkeypatch
razorpay_client.get_client so they never touch the network: test 2 asserts
the API is never even reached for a non-approved promise, test 3 simulates
an API failure and asserts it is logged honestly, never masked as success.

Run with:
    python pipeline/test_promise_reschedule.py
or via pytest:
    python -m pytest pipeline/test_promise_reschedule.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import execute_action
import guardrails
import promise_store
import razorpay_client
import run_case

# Same fixed "today" test_ptp_guardrails.py uses, so window-cap/past-date
# math below is deterministic regardless of when this suite runs.
TODAY = "2026-08-31"


def _case(**overrides) -> dict:
    case = {
        "case_id": "case_ptp_reschedule_0001",
        "today": TODAY,
        "amount": 499.0,
        "decline_code": "issuer_unavailable",
        "payment_rail": "card",
        "email": "test@example.com",
        "contact": "+919876543210",
        "customer_name": "Test Customer",
    }
    case.update(overrides)
    return case


def _promise(promise_id: str, case_id: str, extracted_date: str, customer_id: str = "cust_test_0001") -> dict:
    return {
        "promise_id": promise_id,
        "case_id": case_id,
        "customer_id": customer_id,
        "extracted_date": extracted_date,
    }


def _guardrail_result(case: dict, extracted_date: str) -> dict:
    extraction = {
        "extracted_date": extracted_date,
        "confidence": 0.9,
        "ambiguous": False,
        "clarification_needed": None,
    }
    return guardrails.apply_ptp_guardrails(case, extraction)


# --------------------------------------------------------------------------
# 1. Guardrail-approved promise -> a real test-mode payment link is created
#    and logged (audit row + promises table).
# --------------------------------------------------------------------------
def test_1_approved_promise_creates_real_payment_link():
    case = _case(case_id="case_ptp_reschedule_live_0001")
    extracted_date = "2026-09-05"
    guardrail_result = _guardrail_result(case, extracted_date)
    assert guardrail_result["guardrail_status"] == "approved", (
        f"test fixture bug: expected approved, got {guardrail_result['guardrail_status']!r}"
    )

    promise_id = promise_store.create_promise(case["case_id"], "cust_test_live_0001", "I'll pay on the 5th")
    promise_store.update_promise_extraction(promise_id, extracted_date, 0.9, False)
    promise = _promise(promise_id, case["case_id"], extracted_date, "cust_test_live_0001")

    row = run_case.run_promise_reschedule(case, promise, guardrail_result, source=run_case.SOURCE_MANUAL_TEST)

    assert row["execution_status"] == "success", f"expected success against real test-mode API, got row: {row}"
    assert row["execution_mechanism"].startswith("payment_link_created:")
    assert row["final_action"] == guardrails.PTP_PROCEED_ACTION
    assert row["proposed_action"] == f"reschedule_to_{extracted_date}"

    stored = promise_store.get_promise(promise_id)
    assert stored["payment_link_id"], "payment_link_id was not stored on the promise row"
    assert stored["outcome"] == promise_store.OUTCOME_PENDING, "outcome must stay 'pending' after successful scheduling"
    assert stored["guardrail_status"] is None or stored["guardrail_status"] == "approved"

    print("PASS  test_1_approved_promise_creates_real_payment_link")
    print(f"      execution_mechanism={row['execution_mechanism']!r}")


# --------------------------------------------------------------------------
# 2. Guardrail-rejected promise -> execute_promise_reschedule refuses to run
#    (defensive check) and the Razorpay API is never reached.
# --------------------------------------------------------------------------
def test_2_rejected_promise_never_executes():
    case = _case(case_id="case_ptp_reschedule_rejected_0001")
    extracted_date = "2026-11-15"  # +76 days from TODAY -- past PTP_WINDOW_CAP_DAYS=30
    guardrail_result = _guardrail_result(case, extracted_date)
    assert guardrail_result["guardrail_status"] == "rejected_window_cap", (
        f"test fixture bug: expected rejected_window_cap, got {guardrail_result['guardrail_status']!r}"
    )

    promise = _promise("promise_rejected_0001", case["case_id"], extracted_date)

    call_count = {"n": 0}
    original_get_client = razorpay_client.get_client

    def _fail_if_called():
        call_count["n"] += 1
        raise AssertionError("razorpay_client.get_client() must not be called for a non-approved promise")

    razorpay_client.get_client = _fail_if_called
    try:
        raised = False
        try:
            execute_action.execute_promise_reschedule(case, promise, guardrail_result)
        except ValueError:
            raised = True
        assert raised, "execute_promise_reschedule must raise ValueError for a non-approved guardrail_result"
        assert call_count["n"] == 0, "the Razorpay API must never be reached for a non-approved promise"
    finally:
        razorpay_client.get_client = original_get_client

    print("PASS  test_2_rejected_promise_never_executes")
    print(f"      guardrail_status={guardrail_result['guardrail_status']!r} (routing must skip run_promise_reschedule entirely)")


# --------------------------------------------------------------------------
# 3. Razorpay API failure (mocked client) -> logged honestly as failed, not
#    masked as success; promise record left in a needs-retry state, not a
#    false-positive "scheduled".
# --------------------------------------------------------------------------
class _FailingPaymentLink:
    def create(self, payload):
        raise RuntimeError("simulated Razorpay API failure (network/5xx)")


class _FailingClient:
    def __init__(self):
        self.payment_link = _FailingPaymentLink()


def test_3_api_failure_logged_honestly_not_masked():
    case = _case(case_id="case_ptp_reschedule_fail_0001")
    extracted_date = "2026-09-10"
    guardrail_result = _guardrail_result(case, extracted_date)
    assert guardrail_result["guardrail_status"] == "approved"

    promise_id = promise_store.create_promise(case["case_id"], "cust_test_fail_0001", "I'll pay on the 10th")
    promise_store.update_promise_extraction(promise_id, extracted_date, 0.9, False)
    promise = _promise(promise_id, case["case_id"], extracted_date, "cust_test_fail_0001")

    original_get_client = razorpay_client.get_client
    razorpay_client.get_client = lambda: _FailingClient()
    try:
        row = run_case.run_promise_reschedule(case, promise, guardrail_result, source=run_case.SOURCE_MANUAL_TEST)
    finally:
        razorpay_client.get_client = original_get_client

    assert row["execution_status"] == "failed", f"a Razorpay failure must be logged as failed, got: {row['execution_status']!r}"
    assert row["execution_mechanism"].startswith("execution_failed:")
    assert "simulated Razorpay API failure" in row["execution_detail"]

    stored = promise_store.get_promise(promise_id)
    assert stored["payment_link_id"] is None, "payment_link_id must stay NULL on a failed execution -- no false-positive success"
    assert stored["outcome"] == promise_store.OUTCOME_RESCHEDULE_FAILED, (
        "outcome must move to a clearly-failed state, not stay silently 'pending'"
    )

    print("PASS  test_3_api_failure_logged_honestly_not_masked")


_ALL_TESTS = [
    test_1_approved_promise_creates_real_payment_link,
    test_2_rejected_promise_never_executes,
    test_3_api_failure_logged_honestly_not_masked,
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
