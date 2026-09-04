"""
Regression tests for the "recovered payment still shows a pending PTP
conversation" bug (case pay_TX9ggfR5ekEDTI).

Root cause (see PTP_FLOW.md and the bug writeup this fixes): the Customer
Conversations view built case_summary from only a case_id's FIRST audit row
(webhook_receiver.api_customer_conversations) and the frontend rendered an
"Outreach sent to customer" bubble from that row unconditionally, without
checking whether a PTP offer was ever genuinely made (ptp_offer_decision).
A later 'recovered' row for the same case_id was invisible to both the
backend join and the frontend, so a recovered case rendered as if still
awaiting a reply.

This file exercises, without touching the real demo audit log:
  1. ptp_trigger.should_offer_ptp's new already_recovered veto (pure
     function test).
  2. run_case.case_already_recovered's write/read round trip.
  3. webhook_receiver._case_recovery_info -- the exact function
     api_customer_conversations now uses to detect a case's recovery
     across its FULL row history, not just the first row.
  4. The full scenario: outreach genuinely offered -> payment recovers
     before any reply -> case_already_recovered is True -> a (simulated)
     redelivered payment.failed for the same case_id is vetoed by
     should_offer_ptp (no duplicate outreach) -> the conversation view's
     recovery info reflects resolved, not pending.

WEBHOOK_AUDIT_LOG_PATH is monkeypatched to a temp file for the whole run so
none of this touches logs/webhook_audit_log.csv. customer_history/
promise_store still write to the real local data/customer_history.db, same
uuid-suffixed-customer_id convention tests/test_ptp_trigger.py already uses,
so runs never collide with real traffic.

Run with:
    python tests/test_ptp_recovery_conversation.py
or via pytest:
    python -m pytest tests/test_ptp_recovery_conversation.py -v
"""

from __future__ import annotations

import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import pandas as pd  # noqa: E402

import ptp_trigger  # noqa: E402
import run_case  # noqa: E402
import webhook_receiver  # noqa: E402


def _new_case_id(label: str) -> str:
    return f"pay_test_recovery_{label}_{uuid.uuid4().hex[:8]}"


def _new_customer_key(label: str) -> str:
    return f"cust_test_recovery_{label}_{uuid.uuid4().hex[:8]}"


def test_already_recovered_vetoes_ptp_offer():
    # Otherwise a clean "should offer" case (insufficient_funds, first
    # failure) -- proves case_already_recovered is checked ahead of the
    # True triggers, same precedent hard_decline/open_promise_exists set.
    case = {
        "decline_code": "insufficient_funds",
        "retry_attempt_number": 1,
        "customer_id": f"cust_test_ptptrigger_recovered_{uuid.uuid4().hex[:8]}",
        "ltv_tier": "medium",
        "payment_rail": "card",
        "case_already_recovered": True,
    }

    result = ptp_trigger.should_offer_ptp(case)

    assert result["offer_ptp"] is False
    assert result["trigger_category"] == ptp_trigger.CATEGORY_ALREADY_RECOVERED
    assert result["reason"]
    print("PASS  test_already_recovered_vetoes_ptp_offer")


def test_case_already_recovered_helper_write_read_roundtrip():
    original_path = run_case.WEBHOOK_AUDIT_LOG_PATH
    with tempfile.TemporaryDirectory() as tmpdir:
        run_case.WEBHOOK_AUDIT_LOG_PATH = Path(tmpdir) / "webhook_audit_log.csv"
        try:
            recovered_case_id = _new_case_id("helper_recovered")
            other_case_id = _new_case_id("helper_other")
            customer_key = _new_customer_key("helper")

            assert run_case.case_already_recovered(recovered_case_id) is False

            run_case.run_recovered_case(
                recovered_case_id, 500.0, customer_key, "subscription.charged", source="manual_test"
            )

            assert run_case.case_already_recovered(recovered_case_id) is True
            assert run_case.case_already_recovered(other_case_id) is False
            print("PASS  test_case_already_recovered_helper_write_read_roundtrip")
        finally:
            run_case.WEBHOOK_AUDIT_LOG_PATH = original_path


def test_case_recovery_info_ignores_unrecovered_case():
    df = pd.DataFrame(
        [{"case_id": "pay_x", "final_action": "retry_now", "timestamp": "t1", "amount": 500.0}]
    )
    assert webhook_receiver._case_recovery_info(df) is None
    print("PASS  test_case_recovery_info_ignores_unrecovered_case")


def test_case_recovery_info_finds_recovered_row_after_a_genuine_offer_row():
    # Reproduces the "outreach genuinely sent, then recovered before reply"
    # shape: two rows for the SAME case_id -- first the original
    # payment.failed row with a real PTP offer, then a later
    # subscription.charged recovery row. case_summary (built elsewhere from
    # .iloc[[0]]) would only ever see the first row -- _case_recovery_info
    # is what makes the second row visible to the Conversations view.
    df = pd.DataFrame(
        [
            {
                "case_id": "pay_y",
                "final_action": "retry_now",
                "timestamp": "2026-09-02T10:00:00+00:00",
                "amount": 500.0,
                "ptp_offer_decision": True,
            },
            {
                "case_id": "pay_y",
                "final_action": "recovered",
                "timestamp": "2026-09-02T11:00:00+00:00",
                "amount": 500.0,
                "ptp_offer_decision": None,
            },
        ]
    )
    recovery = webhook_receiver._case_recovery_info(df)
    assert recovery is not None
    assert recovery["recovered"] is True
    assert recovery["recovered_at"] == "2026-09-02T11:00:00+00:00"
    # The first row (case_summary elsewhere) still carries the genuine offer
    # -- confirms OpeningMessage's ptp_offer_decision===true check keeps
    # rendering the outreach bubble for a case that really did get one.
    assert bool(df.iloc[0]["ptp_offer_decision"]) is True
    print("PASS  test_case_recovery_info_finds_recovered_row_after_a_genuine_offer_row")


def test_full_scenario_outreach_then_recovery_then_no_duplicate_outreach():
    """outreach sent -> payment recovers -> conversation shows resolved ->
    no duplicate outreach sent, end to end against the real
    run_case/ptp_trigger code path (audit log monkeypatched to a temp file
    so the real demo log is untouched)."""
    original_path = run_case.WEBHOOK_AUDIT_LOG_PATH
    with tempfile.TemporaryDirectory() as tmpdir:
        run_case.WEBHOOK_AUDIT_LOG_PATH = Path(tmpdir) / "webhook_audit_log.csv"
        try:
            case_id = _new_case_id("full")
            customer_key = _new_customer_key("full")
            now_iso = datetime.now(timezone.utc).isoformat()

            # Step 1: a genuine PTP-offered payment.failed row, built the
            # same shape run_case.run_recovery_case would append (bypassing
            # the full ML-scoring pipeline, which needs no simulating here
            # -- only the audit-row shape matters for this test).
            offer_row = run_case.blank_webhook_audit_row()
            offer_row.update(
                {
                    "case_id": case_id,
                    "timestamp": now_iso,
                    "amount": 500.0,
                    "decline_code": "insufficient_funds",
                    "final_action": "retry_now",
                    "proposed_action": "retry_now",
                    "customer_key": customer_key,
                    "source": "manual_test",
                    "ptp_offer_decision": True,
                    "ptp_trigger_category": ptp_trigger.CATEGORY_INSUFFICIENT_FUNDS_CODE,
                    "ptp_offer_reason": "test setup",
                    "retry_attempt_number": 1,
                }
            )
            run_case.append_webhook_audit_row(offer_row)
            assert run_case.case_already_recovered(case_id) is False

            # Step 2: payment recovers BEFORE the customer ever replies (no
            # promise_store row exists for this case_id at any point in
            # this test).
            run_case.run_recovered_case(case_id, 500.0, customer_key, "subscription.charged", source="manual_test")
            assert run_case.case_already_recovered(case_id) is True

            # Step 3: the conversation view's recovery detection sees it.
            df = pd.read_csv(run_case.WEBHOOK_AUDIT_LOG_PATH)
            match = df[df["case_id"] == case_id]
            recovery = webhook_receiver._case_recovery_info(match)
            assert recovery is not None and recovery["recovered"] is True

            # Step 4: a redelivered/late payment.failed for the SAME
            # case_id must not re-offer PTP -- no duplicate outreach.
            gate_case = {
                "customer_id": customer_key,
                "decline_code": "insufficient_funds",
                "retry_attempt_number": 1,
                "case_already_recovered": run_case.case_already_recovered(case_id),
            }
            result = ptp_trigger.should_offer_ptp(gate_case)
            assert result["offer_ptp"] is False
            assert result["trigger_category"] == ptp_trigger.CATEGORY_ALREADY_RECOVERED

            print("PASS  test_full_scenario_outreach_then_recovery_then_no_duplicate_outreach")
        finally:
            run_case.WEBHOOK_AUDIT_LOG_PATH = original_path


_ALL_TESTS = [
    test_already_recovered_vetoes_ptp_offer,
    test_case_already_recovered_helper_write_read_roundtrip,
    test_case_recovery_info_ignores_unrecovered_case,
    test_case_recovery_info_finds_recovered_row_after_a_genuine_offer_row,
    test_full_scenario_outreach_then_recovery_then_no_duplicate_outreach,
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
