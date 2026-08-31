"""
Promise-to-pay (PTP) honor/break tracking -- Phase 16.

Owns everything downstream of a Phase 14 reschedule (a guardrail-approved
promise with a live Razorpay payment link): matching an incoming webhook
event back to the promise it belongs to, flipping outcome to
honored/broken, recomputing pipeline.customer_ptp_stats, and writing every
state transition to logs/ptp_outcome_log.csv.

Deliberately NOT logged to logs/audit_log.csv -- that file is exclusively
pipeline/run_batch.py's frozen 280-row synthetic-batch output, read by
pipeline/validate_audit_log.py, which asserts an exact row count against
that batch (see run_batch.py's own docstring on this -- webhook_receiver.py
already established the same separation for live webhook rows, writing them
to logs/webhook_audit_log.csv instead). This module's transitions get their
own dedicated file, same pattern as promise_log.csv/webhook_log.csv/
pending_retries.csv.

Three entry points webhook_receiver.py calls into:
  handle_payment_captured(event, event_type) -- payment.captured / payment_link.paid
  handle_payment_failed(event, event_type)   -- payment.failed
  check_expired_promises()                   -- periodic deadline sweep

start_background_expiry_checker() runs check_expired_promises() on a timer
in a daemon thread; it can also be called directly (cron, a manual admin
endpoint, or a test) since check_expired_promises() itself takes no
arguments and is idempotent to call repeatedly.
"""

from __future__ import annotations

import logging
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

import customer_ptp_stats
import promise_store
from guardrails import IST

logger = logging.getLogger("webhook_receiver")

BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
PTP_OUTCOME_LOG_PATH = LOGS_DIR / "ptp_outcome_log.csv"
PTP_OUTCOME_LOG_COLUMNS = [
    "timestamp", "event_type", "promise_id", "case_id", "customer_id",
    "payment_link_id", "extracted_date", "trigger", "reason",
]

EVENT_HONORED = "pending_to_honored"
EVENT_BROKEN = "pending_to_broken"
EVENT_LATE_RECOVERY = "late_recovery_after_broken"
EVENT_IGNORED = "webhook_ignored_non_pending"

# Events that indicate a payment link was paid -- Razorpay fires
# payment.captured for the underlying payment and/or payment_link.paid for
# the link itself; either can arrive first/only depending on how the
# customer paid, so both are treated identically here.
PAYMENT_SUCCESS_EVENTS = {"payment.captured", "payment_link.paid"}
PAYMENT_FAILURE_EVENTS = {"payment.failed"}

DEFAULT_EXPIRY_CHECK_INTERVAL_SECONDS = 300  # 5 minutes -- fine for a demo


def _append_ptp_outcome_log(event_type: str, promise: dict, trigger: str, reason: str) -> None:
    PTP_OUTCOME_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "promise_id": promise.get("promise_id"),
        "case_id": promise.get("case_id"),
        "customer_id": promise.get("customer_id"),
        "payment_link_id": promise.get("payment_link_id"),
        "extracted_date": promise.get("extracted_date"),
        "trigger": trigger,
        "reason": reason,
    }
    df = pd.DataFrame([row], columns=PTP_OUTCOME_LOG_COLUMNS)
    write_header = not PTP_OUTCOME_LOG_PATH.exists()
    df.to_csv(PTP_OUTCOME_LOG_PATH, mode="a", header=write_header, index=False)


# --------------------------------------------------------------------------
# Requirement 1 -- reliable matching. notes.promise_id first (Razorpay echoes
# `notes` back on every webhook event for a payment link and the payment it
# produces -- see execute_action.execute_promise_reschedule, which now sets
# notes={"promise_id": ...} on creation); payment_link_id lookup as a
# fallback for payloads where notes are missing or stripped.
# --------------------------------------------------------------------------
def _extract_promise_match_keys(webhook_payload: dict) -> tuple[str | None, str | None]:
    payload = webhook_payload.get("payload") or {}
    payment_link_entity = ((payload.get("payment_link") or {}).get("entity")) or {}
    payment_entity = ((payload.get("payment") or {}).get("entity")) or {}

    notes = payment_link_entity.get("notes") or payment_entity.get("notes") or {}
    promise_id = notes.get("promise_id") if isinstance(notes, dict) else None

    # payment_link.paid carries the link's own id directly; payment.captured
    # for a payment made via a link carries it as payment_link_id (or, on
    # some API versions, invoice_id) on the payment entity instead.
    payment_link_id = (
        payment_link_entity.get("id")
        or payment_entity.get("payment_link_id")
        or payment_entity.get("invoice_id")
    )
    return promise_id, payment_link_id


def find_any_promise(webhook_payload: dict) -> dict | None:
    """Matches a webhook payload to a promise row regardless of its current
    outcome -- used to detect a late recovery on an already-'broken'
    promise, which find_open_promise (outcome-filtered) would miss."""
    promise_id, payment_link_id = _extract_promise_match_keys(webhook_payload)

    if promise_id:
        promise = promise_store.get_promise(promise_id)
        if promise is not None:
            return promise
        logger.warning(
            "ptp_outcomes: webhook notes.promise_id=%s does not match any promise row -- "
            "falling back to payment_link_id match", promise_id,
        )

    if payment_link_id:
        return promise_store.get_promise_by_payment_link_id(payment_link_id)

    return None


def find_open_promise(webhook_payload: dict) -> dict | None:
    """Same matching as find_any_promise, but only returns a result whose
    outcome is still 'pending' (i.e. genuinely open/unresolved). Used by
    handle_payment_failed, which must never act on an already-resolved
    promise."""
    promise = find_any_promise(webhook_payload)
    if promise is not None and promise["outcome"] == promise_store.OUTCOME_PENDING:
        return promise
    return None


# --------------------------------------------------------------------------
# Requirement 2 -- webhook-driven transitions.
# --------------------------------------------------------------------------
def handle_payment_captured(webhook_payload: dict, event_type: str) -> dict:
    """payment.captured / payment_link.paid handler. Three cases:

      1. Matches an open ('pending') promise -> outcome='honored', stats
         updated, transition logged.
      2. Matches an already-'broken' promise -> a late recovery: money
         arrived, but the promise itself was already broken (the deadline
         passed before payment). outcome deliberately stays 'broken' --
         only late_recovery_at is set. NOT counted as a second resolution
         in customer_ptp_stats (it was already counted broken).
      3. Matches a promise in any other terminal state (already 'honored',
         'no_reply', 'reschedule_failed', ...), or no promise at all -- a
         no-op, logged for visibility (webhook redelivery is expected to
         hit this path and must be idempotent).

    Returns {"matched": bool, "promise_id", "case_id", "transition"}.
    """
    promise = find_any_promise(webhook_payload)
    if promise is None:
        return {"matched": False}

    promise_id = promise["promise_id"]
    case_id = promise["case_id"]

    if promise["outcome"] == promise_store.OUTCOME_PENDING:
        promise_store.mark_promise_honored(promise_id)
        customer_ptp_stats.record_promise_resolution(promise.get("customer_id"), honored=True)
        _append_ptp_outcome_log(
            EVENT_HONORED, promise, event_type,
            f"{event_type} webhook matched open promise -- payment captured before deadline",
        )
        logger.info(
            "ptp_outcomes: promise_id=%s case_id=%s HONORED via %s", promise_id, case_id, event_type,
        )
        return {"matched": True, "promise_id": promise_id, "case_id": case_id, "transition": "honored"}

    if promise["outcome"] == promise_store.OUTCOME_BROKEN:
        promise_store.mark_promise_late_recovery(promise_id)
        _append_ptp_outcome_log(
            EVENT_LATE_RECOVERY, promise, event_type,
            f"{event_type} received after promise already marked broken by the deadline check -- "
            "payment recovered late, outcome intentionally stays 'broken' (see late_recovery_at)",
        )
        logger.info(
            "ptp_outcomes: promise_id=%s case_id=%s LATE RECOVERY via %s (outcome stays broken)",
            promise_id, case_id, event_type,
        )
        return {"matched": True, "promise_id": promise_id, "case_id": case_id, "transition": "late_recovery"}

    _append_ptp_outcome_log(
        EVENT_IGNORED, promise, event_type,
        f"{event_type} received but promise outcome is already {promise['outcome']!r} -- no-op",
    )
    logger.info(
        "ptp_outcomes: promise_id=%s case_id=%s %s received but outcome already %r -- no-op",
        promise_id, case_id, event_type, promise["outcome"],
    )
    return {"matched": True, "promise_id": promise_id, "case_id": case_id, "transition": "no_op"}


def handle_payment_failed(webhook_payload: dict, event_type: str) -> dict:
    """payment.failed handler. Deliberately does NOT mark anything broken --
    a customer may retry the same payment link, and a late-but-eventual
    payment before the deadline must still count as honored. Only
    check_expired_promises() (the deadline sweep) is allowed to move a
    promise to 'broken'. This is purely observational/logged.
    """
    promise = find_open_promise(webhook_payload)
    if promise is None:
        return {"matched": False}

    logger.info(
        "ptp_outcomes: promise_id=%s case_id=%s %s received for open promise -- NOT marking broken "
        "(customer may retry the same link before the deadline); only check_expired_promises() can mark broken.",
        promise["promise_id"], promise["case_id"], event_type,
    )
    return {
        "matched": True,
        "promise_id": promise["promise_id"],
        "case_id": promise["case_id"],
        "transition": "none",
    }


# --------------------------------------------------------------------------
# Requirement 3 -- deadline check.
# --------------------------------------------------------------------------
def check_expired_promises() -> list[dict]:
    """SELECT * FROM promises WHERE outcome='pending' AND payment_link_id
    IS NOT NULL AND extracted_date < today (IST -- the customer's promised
    date is always in IST, same convention guardrails.py/execute_action.py
    use for this field). Every match moves to 'broken', updates
    customer_ptp_stats, and is logged. Safe to call repeatedly (idempotent --
    already-broken rows no longer match the outcome='pending' filter).
    """
    today_iso = datetime.now(IST).date().isoformat()
    expired = promise_store.get_expired_pending_promises(today_iso)

    results = []
    for promise in expired:
        promise_id = promise["promise_id"]
        promise_store.mark_promise_broken(promise_id)
        customer_ptp_stats.record_promise_resolution(promise.get("customer_id"), honored=False)
        reason = f"deadline check: extracted_date={promise['extracted_date']} < today={today_iso} (IST)"
        _append_ptp_outcome_log(EVENT_BROKEN, promise, "deadline_check", reason)
        logger.info(
            "ptp_outcomes: promise_id=%s case_id=%s BROKEN -- %s",
            promise_id, promise["case_id"], reason,
        )
        results.append({"promise_id": promise_id, "case_id": promise["case_id"], "transition": "broken"})

    if results:
        logger.info("ptp_outcomes: check_expired_promises marked %d promise(s) broken", len(results))
    return results


# --------------------------------------------------------------------------
# Background scheduled check -- a daemon thread on a fixed timer, started
# once from webhook_receiver.py's startup. check_expired_promises() itself
# is also directly callable for cron/manual/test use, independent of this
# thread ever running.
# --------------------------------------------------------------------------
_expiry_thread: threading.Thread | None = None
_expiry_stop_event = threading.Event()


def _expiry_loop(interval_seconds: float) -> None:
    while not _expiry_stop_event.wait(interval_seconds):
        try:
            check_expired_promises()
        except Exception:  # noqa: BLE001 -- a sweep failure must never kill the background thread
            logger.exception("ptp_outcomes: check_expired_promises raised in the background expiry loop")


def start_background_expiry_checker(interval_seconds: float = DEFAULT_EXPIRY_CHECK_INTERVAL_SECONDS) -> None:
    """Starts the periodic deadline sweep in a daemon thread -- idempotent,
    a second call is a no-op if the thread is already running. Daemon so it
    never blocks process shutdown."""
    global _expiry_thread
    if _expiry_thread is not None and _expiry_thread.is_alive():
        return
    _expiry_stop_event.clear()
    _expiry_thread = threading.Thread(
        target=_expiry_loop, args=(interval_seconds,), name="ptp-expiry-checker", daemon=True,
    )
    _expiry_thread.start()
    logger.info("ptp_outcomes: background expiry checker started (interval=%ss)", interval_seconds)


def stop_background_expiry_checker() -> None:
    _expiry_stop_event.set()
