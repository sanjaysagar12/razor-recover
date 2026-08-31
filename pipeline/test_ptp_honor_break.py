"""
Phase 16 tests -- PTP honor/break tracking.

Exercises pipeline/ptp_outcomes.py (webhook matching + honored/broken
transitions + the deadline sweep) and pipeline/customer_ptp_stats.py
(the per-customer honor-rate rollup), entirely against simulated webhook
payloads and the local SQLite DB -- no real Razorpay API calls (test-mode
payment-link creation is already covered by test_promise_reschedule.py).

Each test uses a freshly-generated customer_id (uuid4-suffixed) so
customer_ptp_stats assertions are exact regardless of how many times this
suite has run before against the same data/customer_history.db.

Run with:
    python pipeline/test_ptp_honor_break.py
or via pytest:
    python -m pytest pipeline/test_ptp_honor_break.py -v
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import customer_ptp_stats
import promise_store
import ptp_outcomes
from guardrails import IST

TODAY_IST = datetime.now(IST).date()
TODAY_ISO = TODAY_IST.isoformat()
YESTERDAY_ISO = (TODAY_IST - timedelta(days=1)).isoformat()


def _new_customer_id(label: str) -> str:
    return f"cust_test_ptp_{label}_{uuid.uuid4().hex[:8]}"


def _make_scheduled_promise(case_id: str, customer_id: str, extracted_date: str, payment_link_id: str) -> str:
    """Builds a promise row in the same end-state Phase 14's
    run_promise_reschedule leaves on success: extraction stored, a
    payment_link_id attached, outcome still 'pending' (see
    promise_store.update_promise_payment_link's docstring)."""
    promise_id = promise_store.create_promise(case_id, customer_id, f"I'll pay by {extracted_date}")
    promise_store.update_promise_extraction(promise_id, extracted_date, 0.9, False)
    promise_store.update_promise_payment_link(promise_id, payment_link_id)
    stored = promise_store.get_promise(promise_id)
    assert stored["outcome"] == promise_store.OUTCOME_PENDING, "test fixture bug: expected outcome='pending' pre-webhook"
    assert stored["payment_link_id"] == payment_link_id
    return promise_id


def _payment_captured_payload(promise_id: str, payment_link_id: str) -> dict:
    return {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_{uuid.uuid4().hex[:14]}",
                    "amount": 49900,
                    "notes": {"promise_id": promise_id},
                    "payment_link_id": payment_link_id,
                }
            }
        },
    }


# --------------------------------------------------------------------------
# 1. payment.captured for an open promise with extracted_date=today ->
#    outcome flips to 'honored', customer_ptp_stats updates.
# --------------------------------------------------------------------------
def test_1_captured_payment_honors_promise_and_updates_stats():
    customer_id = _new_customer_id("honor")
    payment_link_id = f"plink_honor_{uuid.uuid4().hex[:10]}"
    promise_id = _make_scheduled_promise("case_ptp_honor_0001", customer_id, TODAY_ISO, payment_link_id)

    payload = _payment_captured_payload(promise_id, payment_link_id)
    result = ptp_outcomes.handle_payment_captured(payload, "payment.captured")

    assert result["matched"] is True
    assert result["promise_id"] == promise_id
    assert result["transition"] == "honored"

    stored = promise_store.get_promise(promise_id)
    assert stored["outcome"] == promise_store.OUTCOME_HONORED, f"expected honored, got {stored['outcome']!r}"
    assert stored["resolved_at"], "resolved_at must be set once honored"

    stats = customer_ptp_stats.get_stats(customer_id)
    assert stats is not None, "customer_ptp_stats row was not created"
    assert stats["promises_made"] == 1
    assert stats["promises_honored"] == 1
    assert stats["historical_ptp_honor_rate"] == 1.0

    print("PASS  test_1_captured_payment_honors_promise_and_updates_stats")


def _make_and_expire_broken_promise(label: str) -> tuple[str, str]:
    """Shared fixture for tests 2 and 3: a scheduled promise with
    extracted_date=yesterday, run through check_expired_promises() so it's
    already 'broken'. Returns (promise_id, customer_id)."""
    customer_id = _new_customer_id(label)
    payment_link_id = f"plink_{label}_{uuid.uuid4().hex[:10]}"
    promise_id = _make_scheduled_promise(f"case_ptp_{label}_0001", customer_id, YESTERDAY_ISO, payment_link_id)

    broken = ptp_outcomes.check_expired_promises()
    broken_ids = {r["promise_id"] for r in broken}
    assert promise_id in broken_ids, f"expected {promise_id} in check_expired_promises() results, got {broken_ids}"
    return promise_id, customer_id


# --------------------------------------------------------------------------
# 2. check_expired_promises() for a promise with extracted_date=yesterday ->
#    outcome flips to 'broken', customer_ptp_stats updates.
# --------------------------------------------------------------------------
def test_2_expired_promise_marked_broken_and_stats_updated():
    promise_id, customer_id = _make_and_expire_broken_promise("broken")

    stored = promise_store.get_promise(promise_id)
    assert stored["outcome"] == promise_store.OUTCOME_BROKEN, f"expected broken, got {stored['outcome']!r}"
    assert stored["resolved_at"], "resolved_at must be set once broken"

    stats = customer_ptp_stats.get_stats(customer_id)
    assert stats is not None
    assert stats["promises_made"] == 1
    assert stats["promises_honored"] == 0
    assert stats["historical_ptp_honor_rate"] == 0.0

    print("PASS  test_2_expired_promise_marked_broken_and_stats_updated")


# --------------------------------------------------------------------------
# 3. A payment captured AFTER a promise was already marked 'broken' by the
#    deadline check must NOT flip outcome back to 'honored'. Intended
#    behavior (per Phase 16 brief): log it as a late recovery in a separate
#    field (late_recovery_at), leave outcome='broken', and do not double-
#    count it in customer_ptp_stats (it was already counted broken).
# --------------------------------------------------------------------------
def test_3_late_payment_after_broken_does_not_flip_back_to_honored():
    promise_id, customer_id = _make_and_expire_broken_promise("late_recovery")
    stored_before = promise_store.get_promise(promise_id)
    assert stored_before["outcome"] == promise_store.OUTCOME_BROKEN
    assert stored_before["late_recovery_at"] is None

    payload = _payment_captured_payload(promise_id, stored_before["payment_link_id"])
    result = ptp_outcomes.handle_payment_captured(payload, "payment.captured")

    assert result["matched"] is True
    assert result["transition"] == "late_recovery"

    stored_after = promise_store.get_promise(promise_id)
    assert stored_after["outcome"] == promise_store.OUTCOME_BROKEN, (
        f"outcome must stay 'broken' after a late recovery, got {stored_after['outcome']!r}"
    )
    assert stored_after["late_recovery_at"], "late_recovery_at must be set once money arrives late"

    # Not double-counted: still exactly the 1 resolution test 2 recorded.
    stats = customer_ptp_stats.get_stats(customer_id)
    assert stats["promises_made"] == 1, "a late recovery on an already-broken promise must not re-increment promises_made"
    assert stats["promises_honored"] == 0
    assert stats["historical_ptp_honor_rate"] == 0.0

    print("PASS  test_3_late_payment_after_broken_does_not_flip_back_to_honored")


# --------------------------------------------------------------------------
# 4. Requirement 1's fallback path: a webhook payload with no `notes` at
#    all must still match via payment_link_id.
# --------------------------------------------------------------------------
def test_4_matching_falls_back_to_payment_link_id_when_notes_missing():
    customer_id = _new_customer_id("fallback")
    payment_link_id = f"plink_fallback_{uuid.uuid4().hex[:10]}"
    promise_id = _make_scheduled_promise("case_ptp_fallback_0001", customer_id, TODAY_ISO, payment_link_id)

    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_{uuid.uuid4().hex[:14]}",
                    "amount": 49900,
                    "payment_link_id": payment_link_id,
                    # deliberately no "notes" key at all
                }
            }
        },
    }

    matched = ptp_outcomes.find_open_promise(payload)
    assert matched is not None, "payment_link_id fallback match failed"
    assert matched["promise_id"] == promise_id

    result = ptp_outcomes.handle_payment_captured(payload, "payment.captured")
    assert result["transition"] == "honored"

    print("PASS  test_4_matching_falls_back_to_payment_link_id_when_notes_missing")


_ALL_TESTS = [
    test_1_captured_payment_honors_promise_and_updates_stats,
    test_2_expired_promise_marked_broken_and_stats_updated,
    test_3_late_payment_after_broken_does_not_flip_back_to_honored,
    test_4_matching_falls_back_to_payment_link_id_when_notes_missing,
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
