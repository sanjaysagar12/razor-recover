"""
Phase 15 tests -- promise-to-pay clarification loop.

Drives the real /api/promise-reply Flask route (webhook_receiver.app.test_client())
end to end, the same route real customer replies hit, so the clarification-
round bookkeeping is exercised through actual HTTP request/response cycles,
not just the internal helper in isolation. Two things are stubbed, same
reasoning as test_ptp_guardrails.py / test_promise_reschedule.py:

  - llm_layer.extract_promise_date is monkeypatched to return a canned,
    schema-shaped extraction per message text instead of calling a real LLM
    provider -- this suite tests the clarification-loop branching (Phase 15),
    not Phase 10's NLP correctness.
  - razorpay_client.get_client is monkeypatched so the clean-message test
    never makes a real network call, and the vague-message test asserts the
    Razorpay API is never even reached (a clarification/fallback reply must
    never create a payment link).

Run with:
    python tests/test_promise_clarification.py
or via pytest:
    python -m pytest tests/test_promise_clarification.py -v
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "pipeline"))

# promise_store/razorpay_client bare (not `from pipeline import ...`) so
# these are the exact same module objects webhook_receiver.py/execute_action.py
# bare-import internally -- required for the razorpay_client.get_client
# monkeypatches below to actually reach the /api/promise-reply code path
# under test instead of silently no-op'ing against a separate copy.
import promise_store  # noqa: E402
import razorpay_client  # noqa: E402
import webhook_receiver  # noqa: E402


def _install_canned_extraction(canned_by_message: dict) -> callable:
    """Monkeypatches webhook_receiver.llm_layer.extract_promise_date to
    return a canned dict per message text, bypassing the real LLM call.
    Returns the original function so the caller can restore it."""
    original = webhook_receiver.llm_layer.extract_promise_date

    def fake_extract(message, case_context, adapter=None):
        canned = canned_by_message[message]
        return {
            **canned,
            "model_version": "mock:phase15-test",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    webhook_receiver.llm_layer.extract_promise_date = fake_extract
    return original


class _FakePaymentLink:
    def create(self, payload):
        return {"id": "plink_test_phase15_fake", "short_url": "https://rzp.io/phase15_fake"}


class _FakeSucceedingClient:
    def __init__(self):
        self.payment_link = _FakePaymentLink()


def _install_case_context_with_amount(amount: float, decline_code: str) -> callable:
    """webhook_receiver._promise_date_case_context only carries amount/
    decline_code when case_id already has a matching row in
    logs/webhook_audit_log.csv (a real customer reply follows a payment-
    failure case that already went through the main pipeline and got
    logged there). This test's case_id is synthetic and has no such row, so
    execute_action.execute_promise_reschedule's case["amount"] lookup would
    KeyError -- wrap the real function to inject amount/decline_code the
    same way a real webhook-originated case_context would carry them."""
    original = webhook_receiver._promise_date_case_context

    def fake_case_context(case_id):
        context = original(case_id)
        context.setdefault("amount", amount)
        context.setdefault("decline_code", decline_code)
        return context

    webhook_receiver._promise_date_case_context = fake_case_context
    return original


def _post_promise_reply(client, case_id: str, customer_id: str, message: str):
    resp = client.post(
        "/api/promise-reply",
        json={"case_id": case_id, "customer_id": customer_id, "message": message},
    )
    return resp.status_code, resp.get_json()


# --------------------------------------------------------------------------
# 1. Three consecutive vague replies for the same case_id -> clarification
#    rounds increment 1, 2, then cap at 2 with an automatic fallback -- the
#    loop must never ask a third time or exceed MAX_CLARIFICATION_ROUNDS.
# --------------------------------------------------------------------------
def test_1_three_vague_messages_cap_clarification_at_two_rounds():
    case_id = f"case_clarify_{uuid.uuid4().hex[:8]}"
    # uuid-suffixed (not a fixed literal) so promise_store.has_open_promise
    # (webhook_receiver.api_promise_reply's PTP-offer gate) never sees a
    # leftover unresolved promise from an earlier run of this same test --
    # same isolation convention test_risk_tier.py/test_ptp_trigger.py etc.
    # already use for every customer_id they create.
    customer_id = f"cust_clarify_{uuid.uuid4().hex[:8]}"
    messages = ["soon", "maybe", "I'll try"]
    canned = {
        "soon": {
            "extracted_date": None, "confidence": 0.1, "ambiguous": True,
            "clarification_needed": "When exactly can you pay?",
        },
        "maybe": {
            "extracted_date": None, "confidence": 0.15, "ambiguous": True,
            "clarification_needed": "Could you give me an exact date?",
        },
        "I'll try": {
            "extracted_date": None, "confidence": 0.05, "ambiguous": True,
            "clarification_needed": "I need a specific date to schedule this.",
        },
    }

    original_extract = _install_canned_extraction(canned)
    original_get_client = razorpay_client.get_client

    def _fail_if_called():
        raise AssertionError("Razorpay must never be reached for a clarification/fallback reply")

    razorpay_client.get_client = _fail_if_called
    client = webhook_receiver.app.test_client()
    try:
        results = []
        for message in messages:
            status_code, data = _post_promise_reply(client, case_id, customer_id, message)
            assert status_code == 200, data
            results.append(data)
    finally:
        webhook_receiver.llm_layer.extract_promise_date = original_extract
        razorpay_client.get_client = original_get_client

    # Round 1 -- clarifying, a follow-up question is generated.
    assert results[0]["status"] == promise_store.STATUS_CLARIFYING
    assert results[0]["clarification_round"] == 1
    assert results[0]["follow_up_message"]

    # Round 2 -- clarifying again, a second follow-up question.
    assert results[1]["status"] == promise_store.STATUS_CLARIFYING
    assert results[1]["clarification_round"] == 2
    assert results[1]["follow_up_message"]

    # Round 3 -- cap reached: no further question, fallback fires instead.
    assert results[2]["status"] == promise_store.STATUS_FALLBACK
    assert results[2]["clarification_round"] == 2, "clarification_round must never exceed MAX_CLARIFICATION_ROUNDS"
    assert results[2].get("follow_up_message") is None
    assert results[2]["fallback"] is not None
    assert results[2]["fallback"]["fallback_mechanism"] in ("predictor", "fixed_default_24h")
    assert results[2]["fallback"]["scheduled_for"]

    # Cross-check against the promises table -- three separate rows (one per
    # reply, per promise_store.create_promise's "one row per reply" design),
    # rounds/statuses strictly following the cap.
    stored_rows = [promise_store.get_promise(r["promise_id"]) for r in results]
    assert [row["clarification_round"] for row in stored_rows] == [1, 2, 2]
    assert [row["status"] for row in stored_rows] == [
        promise_store.STATUS_CLARIFYING, promise_store.STATUS_CLARIFYING, promise_store.STATUS_FALLBACK,
    ]
    assert stored_rows[2]["outcome"] == promise_store.OUTCOME_NO_REPLY

    print("PASS  test_1_three_vague_messages_cap_clarification_at_two_rounds")
    for i, row in enumerate(stored_rows, start=1):
        print(f"      reply {i}: promise_id={row['promise_id']} status={row['status']} round={row['clarification_round']}")


# --------------------------------------------------------------------------
# 2. One clear, unambiguous, confident message -> schedules immediately via
#    the unchanged Phase 11-14 guardrail/reschedule path, clarification_round
#    stays at 0.
# --------------------------------------------------------------------------
def test_2_clear_message_schedules_immediately_round_stays_zero():
    case_id = f"case_clarify_clean_{uuid.uuid4().hex[:8]}"
    customer_id = f"cust_clarify_clean_{uuid.uuid4().hex[:8]}"
    message = "I'll pay on the 5th"
    canned = {
        message: {
            "extracted_date": "2026-09-05", "confidence": 0.9, "ambiguous": False,
            "clarification_needed": None,
        },
    }

    original_extract = _install_canned_extraction(canned)
    original_case_context = _install_case_context_with_amount(499.0, "issuer_unavailable")
    original_get_client = razorpay_client.get_client
    razorpay_client.get_client = lambda: _FakeSucceedingClient()
    client = webhook_receiver.app.test_client()
    try:
        status_code, data = _post_promise_reply(client, case_id, customer_id, message)
    finally:
        webhook_receiver.llm_layer.extract_promise_date = original_extract
        webhook_receiver._promise_date_case_context = original_case_context
        razorpay_client.get_client = original_get_client

    assert status_code == 200, data
    assert data["ambiguous"] is False
    assert data["guardrail_status"] == "approved"
    assert data["reschedule"]["execution_status"] == "success"

    stored = promise_store.get_promise(data["promise_id"])
    assert stored["clarification_round"] == 0, "non-ambiguous path must never touch clarification_round"
    assert stored["status"] == promise_store.STATUS_SCHEDULED
    assert stored["payment_link_id"] == "plink_test_phase15_fake"

    print("PASS  test_2_clear_message_schedules_immediately_round_stays_zero")
    print(f"      promise_id={data['promise_id']} status={stored['status']} round={stored['clarification_round']}")


_ALL_TESTS = [
    test_1_three_vague_messages_cap_clarification_at_two_rounds,
    test_2_clear_message_schedules_immediately_round_stays_zero,
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
