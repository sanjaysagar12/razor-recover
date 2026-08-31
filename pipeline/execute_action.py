"""
Executes (or deliberately doesn't) the pipeline's final_action against the
real Razorpay account, for live webhook cases only -- never called from the
synthetic batch simulation (run_batch.py), which only ever logs a
hypothetical decision.

Why this isn't a literal "retry API" call for retry_now: Razorpay has no
merchant-facing API to force-retry a failed payment or force-charge a
subscription. Automatic retries are scheduled and executed by Razorpay/NPCI
on their own T+1/T+2/T+3 cadence; the dashboard's "Charge this now" button is
a test-mode-only manual control, not an API (confirmed against
https://razorpay.com/docs/payments/subscriptions/payment-retries/ and the
installed razorpay SDK's method list -- no retry/force-charge method exists
on client.payment or client.subscription). The one real, callable action
that actually causes a retry to happen is creating + sending a Payment Link
(client.payment_link.create, POST /v1/payment_links) -- so that's what
retry_now executes. This is a deliberate substitution, not a shortcut: see
_RETRY_NOW_EXPLANATION below, which is written into every retry_now
execution's audit detail so the discrepancy between "recommended_action" and
"what actually ran" is never silently lost.

Every function here returns a result dict (never raises) and every Razorpay
API call is wrapped in try/except -- a failed execution must show up as
execution_status="failed" in the audit trail, not crash the webhook handler.
"""

from __future__ import annotations

import csv
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import razorpay_client
from guardrails import IST

BASE_DIR = Path(__file__).resolve().parent.parent
PENDING_RETRIES_PATH = BASE_DIR / "logs" / "pending_retries.csv"
PENDING_RETRIES_COLUMNS = ["case_id", "customer_key", "scheduled_for", "logged_at"]

_RETRY_NOW_EXPLANATION = (
    "retry_now executed via payment link -- Razorpay has no card force-retry API; "
    "automatic network-level retries also continue on their own T+1-T+3 schedule independently."
)

# prompt_alt_payment has no dedicated Razorpay flow either -- a payment link
# lets the customer pick a different method at checkout, which is the
# closest real action available, so it's routed through the same mechanism
# as retry_now.
_ACTIONABLE_FINAL_ACTIONS = {"retry_now", "prompt_alt_payment"}
_TERMINAL_FINAL_ACTIONS = {"no_retry_prompt_update", "escalate_human"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result(status: str, mechanism: str, detail: str, timestamp: str) -> dict:
    return {
        "execution_status": status,
        "execution_mechanism": mechanism,
        "execution_detail": detail,
        "execution_timestamp": timestamp,
    }


def _send_payment_link(case: dict, reason_label: str, timestamp: str) -> dict:
    explanation = _RETRY_NOW_EXPLANATION if reason_label == "retry_now" else (
        "prompt_alt_payment executed via payment link -- lets the customer pick a different "
        "payment method at checkout; Razorpay has no dedicated 'switch payment method' API."
    )
    try:
        client = razorpay_client.get_client()
        amount_paise = int(round(float(case["amount"]) * 100))
        payload = {
            "amount": amount_paise,
            "currency": "INR",
            "description": f"Payment recovery ({reason_label}) for case {case['case_id']}",
            "customer": {
                "name": case.get("customer_name") or "Customer",
                "email": case.get("email") or "",
                "contact": case.get("contact") or "",
            },
            "notify": {"sms": bool(case.get("contact")), "email": bool(case.get("email"))},
            "reference_id": case["case_id"],
        }
        link = client.payment_link.create(payload)
        detail = f"{explanation} payment_link_id={link.get('id')} short_url={link.get('short_url')}"
        return _result("success", "payment_link_sent", detail, timestamp)
    except Exception as exc:  # noqa: BLE001 -- any SDK/network failure must degrade to a logged failure, not crash
        detail = f"{explanation} payment_link creation FAILED: {type(exc).__name__}: {exc}"
        return _result("failed", "payment_link_sent", detail, timestamp)


def _append_pending_retry(case_id: str, customer_key: str | None, scheduled_for: str | None, timestamp: str) -> None:
    PENDING_RETRIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_header = not PENDING_RETRIES_PATH.exists()
    with open(PENDING_RETRIES_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PENDING_RETRIES_COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerow(
            {
                "case_id": case_id,
                "customer_key": customer_key or "",
                "scheduled_for": scheduled_for or "",
                "logged_at": timestamp,
            }
        )


def _schedule_retry(case: dict, action_scheduled_for: str | None, timestamp: str) -> dict:
    note = (
        "retry_scheduled logged to logs/pending_retries.csv -- no scheduler/cron built yet "
        "(known limitation for this phase, not building one today)."
    )
    try:
        _append_pending_retry(case["case_id"], case.get("customer_key"), action_scheduled_for, timestamp)
        detail = f"{note} scheduled_for={action_scheduled_for}"
        return _result("scheduled_logged", "pending_retries_logged", detail, timestamp)
    except Exception as exc:  # noqa: BLE001
        detail = f"{note} FAILED to write pending_retries row: {type(exc).__name__}: {exc}"
        return _result("failed", "pending_retries_logged", detail, timestamp)


def execute_action(case: dict, final_action: str, action_scheduled_for: str | None = None) -> dict:
    """case: case_facts (must include case_id, amount; customer_name/email/
    contact/customer_key used opportunistically if present). final_action:
    guardrails.apply_guardrails()'s final_action for this case.
    action_scheduled_for: the proposal's scheduled timestamp, only used for
    retry_scheduled.

    Returns {execution_status, execution_mechanism, execution_detail,
    execution_timestamp} -- a fact distinct from (and to be logged alongside,
    never merged into) the decision fields, since a decision and its
    execution can diverge (e.g. final_action=retry_now but the Razorpay API
    call fails).
    """
    timestamp = _now()

    if final_action in _ACTIONABLE_FINAL_ACTIONS:
        return _send_payment_link(case, final_action, timestamp)

    if final_action == "retry_scheduled":
        return _schedule_retry(case, action_scheduled_for, timestamp)

    if final_action in _TERMINAL_FINAL_ACTIONS:
        detail = f"final_action={final_action} is terminal -- no Razorpay action taken."
        return _result("not_applicable", "none", detail, timestamp)

    detail = f"Unrecognized final_action={final_action!r} -- no action taken."
    return _result("not_applicable", "none", detail, timestamp)


# --------------------------------------------------------------------------
# Phase 14 -- PTP (Promise-to-Pay) reschedule execution.
#
# Same "no dedicated Razorpay API for this, a Payment Link is the closest
# real, callable action" substitution _send_payment_link makes for
# retry_now/prompt_alt_payment above -- there is no merchant-facing
# "reschedule this promise" endpoint either, so a fresh Payment Link is
# created with expire_by set to the end of the customer's promised day
# (IST), and its id is what gets stored back onto the promise record.
# --------------------------------------------------------------------------

_PTP_RESCHEDULE_EXPLANATION = (
    "schedule_ptp_payment_link executed via payment link -- Razorpay has no dedicated "
    "'reschedule a promise' API; a fresh Payment Link with expire_by set to end-of-day "
    "(IST) on the customer's promised date is the closest real, callable action."
)


def _date_to_ist_eod_epoch(date_str: str) -> int:
    """YYYY-MM-DD -> unix epoch seconds for 23:59:59 IST on that date -- used
    as payment_link.create's expire_by so the link stays valid through the
    whole promised day in the customer's own calendar, not the server's UTC
    one (same IST-local convention guardrails.py's PTP rules already use)."""
    d = date.fromisoformat(date_str)
    eod_ist = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=IST)
    return int(eod_ist.timestamp())


def execute_promise_reschedule(case: dict, promise: dict, guardrail_result: dict) -> dict:
    """Executes a guardrail-APPROVED promise-to-pay date by creating a real
    Razorpay Payment Link (client.payment_link.create), the same API call
    _send_payment_link makes above, just with expire_by anchored to the
    promised date instead of amount/description alone.

    case: case-shaped dict for promise["case_id"] -- amount is required;
    customer_name/email/contact are used opportunistically if present, same
    as _send_payment_link. promise: a pipeline.promise_store row (must
    include promise_id, case_id, extracted_date). guardrail_result:
    guardrails.apply_ptp_guardrails' output for this promise.

    Only ever called for guardrail_status == "approved" -- rejected /
    pending_clarification promises must route to human review or a
    clarification reply instead (the caller's decision, see
    webhook_receiver.py's /api/promise-reply). This function still refuses
    to run for anything else (raises ValueError) as a second, defensive
    check against a caller-side routing bug -- it must never silently
    schedule a payment link for a promise the guardrails did not approve.

    Returns {execution_status, execution_mechanism, execution_detail,
    execution_timestamp, payment_link_id} -- payment_link_id is None on
    failure. Past the guardrail-status check, this never raises: any
    Razorpay SDK/network failure is caught and returned as
    execution_status="failed", same contract as _send_payment_link (a failed
    execution must show up as a logged failure, never a crash or a
    false-positive success).
    """
    guardrail_status = guardrail_result.get("guardrail_status")
    if guardrail_status != "approved":
        raise ValueError(
            f"execute_promise_reschedule called with guardrail_status={guardrail_status!r} for "
            f"promise_id={promise.get('promise_id')!r} -- only 'approved' promises may be scheduled; "
            "rejected/pending_clarification promises must route to human review or a clarification "
            "reply instead, never to this function."
        )

    timestamp = _now()
    extracted_date = promise.get("extracted_date")

    try:
        client = razorpay_client.get_client()
        amount_paise = int(round(float(case["amount"]) * 100))
        payload = {
            "amount": amount_paise,
            "currency": "INR",
            "description": f"Promise-to-pay reschedule for case {promise['case_id']} (promised {extracted_date})",
            "customer": {
                "name": case.get("customer_name") or "Customer",
                "email": case.get("email") or "",
                "contact": case.get("contact") or "",
            },
            "notify": {"sms": bool(case.get("contact")), "email": bool(case.get("email"))},
            # promise_id alone (a UUID4, 36 chars) -- Razorpay caps reference_id at
            # 40 chars, which "{case_id}:{promise_id}" can exceed; promise_id is
            # already globally unique on its own (see promise_store.create_promise).
            "reference_id": promise["promise_id"],
            "expire_by": _date_to_ist_eod_epoch(extracted_date),
        }
        link = client.payment_link.create(payload)
        link_id = link.get("id")
        detail = f"{_PTP_RESCHEDULE_EXPLANATION} payment_link_id={link_id} short_url={link.get('short_url')}"
        return {
            "execution_status": "success",
            "execution_mechanism": f"payment_link_created:{link_id}",
            "execution_detail": detail,
            "execution_timestamp": timestamp,
            "payment_link_id": link_id,
        }
    except Exception as exc:  # noqa: BLE001 -- any SDK/network failure must degrade to a logged failure, not crash
        detail = f"{_PTP_RESCHEDULE_EXPLANATION} payment_link creation FAILED: {type(exc).__name__}: {exc}"
        return {
            "execution_status": "failed",
            "execution_mechanism": f"execution_failed:{type(exc).__name__}: {exc}",
            "execution_detail": detail,
            "execution_timestamp": timestamp,
            "payment_link_id": None,
        }
