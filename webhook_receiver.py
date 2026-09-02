"""
Razorpay webhook receiver for the AI Revenue Recovery pipeline.

Listens on 0.0.0.0:5555 (tunneled at https://razor-recover.heracle.fit via
Cloudflare Tunnel). Verifies X-Razorpay-Signature against the raw request
body, maps the event into the pipeline's case schema, and runs it through
confidence_gate -> shap_extract -> (conditionally) llm_layer -> guardrails ->
execute_action, writing one row per case to logs/webhook_audit_log.csv.

logs/audit_log.csv (25 columns) is exclusively pipeline/run_batch.py's
synthetic-batch output -- it's read by pipeline/validate_audit_log.py, which
asserts its row count matches the 280-row historical batch and merges it
against that batch's ground-truth outcomes. Mixing live webhook rows into it
breaks both of those. Live cases are appended to a separate file,
logs/webhook_audit_log.csv, with the same first 25 columns (identical
schema/order to AUDIT_COLUMNS) plus additional columns appended at the end
for decline-code-mapper output, customer-history source, and action
execution results.

Run:
    python webhook_receiver.py
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import json
import logging
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = BASE_DIR / "pipeline"
MODELS_DIR_PATH = BASE_DIR / "models"
sys.path.insert(0, str(PIPELINE_DIR))
sys.path.insert(0, str(MODELS_DIR_PATH))

import confidence_gate  # noqa: E402
import customer_directory  # noqa: E402
import customer_history  # noqa: E402
import customer_ptp_stats  # noqa: E402
import dashboard_training  # noqa: E402
import decline_code_mapper  # noqa: E402
import execute_action  # noqa: E402
import guardrails  # noqa: E402
import llm_layer  # noqa: E402
import promise_store  # noqa: E402
import ptp_outcomes  # noqa: E402
import ptp_trigger  # noqa: E402
import run_batch  # noqa: E402
import run_case  # noqa: E402
import train_tree_models  # noqa: E402

load_dotenv()

import os  # noqa: E402

RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
PORT = 5555

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "webhook_receiver.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("webhook_receiver")

# --------------------------------------------------------------------------
# Task 1 -- webhook transport-layer log (separate from the pipeline-decision
# log, logs/webhook_audit_log.csv). Captures EVERY POST to /webhook/razorpay,
# including ones that never reach the pipeline at all: signature rejections,
# malformed JSON, and unhandled event types. Those previously only showed up
# in the free-text webhook_receiver.log, not in any structured, queryable
# form -- this is that missing structured record.
# --------------------------------------------------------------------------
WEBHOOK_LOG_PATH = LOGS_DIR / "webhook_log.csv"
WEBHOOK_LOG_COLUMNS = ["timestamp", "event_type", "signature_valid", "case_id", "outcome", "error_detail"]

OUTCOME_PROCESSED = "processed"
OUTCOME_IGNORED = "ignored"
OUTCOME_SIGNATURE_REJECTED = "signature_rejected"
OUTCOME_ERROR = "error"


def _append_webhook_log(
    event_type: str | None, signature_valid: bool, case_id: str | None, outcome: str, error_detail: str | None = None
) -> None:
    WEBHOOK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "signature_valid": signature_valid,
        "case_id": case_id,
        "outcome": outcome,
        "error_detail": error_detail,
    }
    df = pd.DataFrame([row], columns=WEBHOOK_LOG_COLUMNS)
    write_header = not WEBHOOK_LOG_PATH.exists()
    df.to_csv(WEBHOOK_LOG_PATH, mode="a", header=write_header, index=False)


# --------------------------------------------------------------------------
# Event routing
# --------------------------------------------------------------------------
RECOVERY_EVENTS = {"payment.failed", "subscription.pending", "subscription.halted"}
RECOVERED_EVENT = "subscription.charged"

# Phase 16 -- PTP honor/break tracking. Independent of RECOVERY_EVENTS/
# RECOVERED_EVENT above: a payment.captured/payment_link.paid event for a
# Phase 14 promise-to-pay link has nothing to do with the decline-recovery
# pipeline (no decline code, no subscription retry), so it's routed
# separately rather than through process_recovery_case/process_recovered_case.
# payment.failed is already in RECOVERY_EVENTS and keeps going through the
# full pipeline unchanged below -- ptp_outcomes.handle_payment_failed is
# called alongside that, purely for its own (non-mutating) logging.
PTP_HONOR_EVENTS = {"payment.captured", "payment_link.paid"}

# --------------------------------------------------------------------------
# Decline-code / payment-rail mapping
#
# The model (pipeline/shap_extract.py, data/generate_synthetic.py) was
# trained on a small SYNTHETIC decline-code taxonomy, not Razorpay's real
# error_reason vocabulary. There is no 1:1 mapping between the two -- this
# is a best-effort mapping to the nearest synthetic bucket. Unrecognized
# reasons fall back to "generic_decline" (AMBIGUOUS bucket, which always
# routes to the LLM layer rather than being auto-actioned on a guess).
# --------------------------------------------------------------------------
RAZORPAY_REASON_TO_DECLINE_CODE = {
    "insufficient_balance": "insufficient_funds",
    "insufficient_funds": "insufficient_funds",
    "51": "51_insufficient_funds",
    "card_expired": "expired_card_soft",
    "expired_card": "expired_card_soft",
    "stolen_card": "stolen_card",
    "pickup_card": "stolen_card",
    "lost_card": "lost_card",
    "restricted_card": "restricted_card",
    "invalid_account_number": "invalid_account",
    "invalid_account": "invalid_account",
    "account_closed": "invalid_account",
    "do_not_honor": "05_do_not_honor",
    "issuer_unavailable": "issuer_unavailable",
    "issuer_not_available": "issuer_unavailable",
    "bank_server_error": "issuer_unavailable",
    "issuer_declined": "generic_decline",
    "payment_failed": "generic_decline",
    "processing_error": "generic_decline",
    "gateway_error": "generic_decline",
    "authentication_failed": "generic_decline",
    "risk_check_failed": "generic_decline",
    "card_declined": "generic_decline",
}

# decline_code_bucket (the model's categorical feature) and the ambiguous-
# routing flag are now both computed authoritatively by
# pipeline/decline_code_mapper.py from the real error_reason -- see
# map_payload_to_case below. RAZORPAY_REASON_TO_DECLINE_CODE above still
# feeds the separate `decline_code` field (kept in the synthetic vocabulary
# since pipeline/guardrails.py's HARD_DECLINE_CODES matches on those exact
# literal strings -- see the Known limitations note in README_WEBHOOK.md
# about hard_decline_excluded not reliably firing on real traffic).

# Model was trained on exactly these three rails (data/generate_synthetic.py).
# Razorpay's `method` field has more values (netbanking, wallet, ...); those
# fall back to "card" with a logged warning since the model has no category
# for them.
PAYMENT_METHOD_TO_RAIL = {
    "emandate": "emandate",
    "nach": "emandate",
    "card": "card",
    "upi": "upi_autopay",
}

# --------------------------------------------------------------------------
# Synthetic defaults for the model features that have NO source at all --
# not in the webhook payload, not derivable, not something customer_history
# tracks (see pipeline/customer_history.py for the fields that DO have real
# per-customer tracking now: customer_tenure_days, ltv_tier,
# historical_ptp_honor_rate, prior_retry_success_count). Values are the
# midpoint/median of the distributions used to generate data/train.csv +
# data/holdout.csv (see data/generate_synthetic.py) -- NOT derived from any
# real customer. Every use is logged.
# --------------------------------------------------------------------------
DEFAULT_HOURS_SINCE_LAST_ATTEMPT = 24.0
DEFAULT_ISSUER_BANK_RISK_TIER = "medium_risk"
DEFAULT_AMOUNT_VS_HISTORICAL_AVG = 1.0

IST = timezone(timedelta(hours=5, minutes=30))
NPCI_PEAK_START_HOUR = 10
NPCI_PEAK_END_HOUR = 13  # exclusive -- [10:00, 13:00) IST, same window guardrails.py enforces


def _customer_key(customer_id: str | None, subscription_id: str | None) -> str | None:
    """customer_id when the webhook payload has one; else a subscription-
    scoped fallback key (subscriptions carry a stable id even when the
    payment entity itself has no customer_id attached); else None if neither
    is present -- callers must handle that case (no key to track history
    against)."""
    if customer_id:
        return customer_id
    if subscription_id:
        return f"sub:{subscription_id}"
    return None


def _time_of_day_bucket(hour: int) -> str:
    if 4 <= hour < 8:
        return "early_morning"
    if 8 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def _extract_entities(event: dict) -> tuple[dict, dict]:
    payload = event.get("payload") or {}
    payment_entity = ((payload.get("payment") or {}).get("entity")) or {}
    subscription_entity = ((payload.get("subscription") or {}).get("entity")) or {}
    return payment_entity, subscription_entity


def _extract_case_id(event: dict) -> str:
    payment_entity, subscription_entity = _extract_entities(event)
    return payment_entity.get("id") or subscription_entity.get("id") or f"webhook_{int(time.time() * 1000)}"


# --------------------------------------------------------------------------
# Signature verification
# --------------------------------------------------------------------------
def verify_razorpay_signature(raw_body: bytes, received_signature: str, secret: str) -> bool:
    """HMAC-SHA256 of the raw (unparsed) request body, hex-encoded, compared
    to the X-Razorpay-Signature header with a constant-time comparison.
    Must run against raw bytes -- re-serializing parsed JSON before hashing
    can produce a different byte string (key order, whitespace, unicode
    escaping) and silently break verification.
    """
    if not received_signature or not secret:
        return False
    expected_signature = hmac.new(
        key=secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_signature, received_signature)


# --------------------------------------------------------------------------
# Payload -> case schema mapping
# --------------------------------------------------------------------------
def map_payload_to_case(event: dict) -> dict:
    """Maps a verified Razorpay webhook event body into the pipeline's case
    schema (same feature set as pipeline/shap_extract.py's feature_columns
    plus decline_code/case_id/amount/etc.). Returns a dict usable directly
    as case_facts for shap_extract.score_new_case / confidence_gate.route_case
    / guardrails.apply_guardrails / run_batch.build_audit_row.

    Has one side effect: on a customer seen for the first time, this creates
    a new pipeline.customer_history record (see that module's
    get_or_create_customer). The case dict's customer_history_source field
    tells you when that happened.
    """
    payment_entity, subscription_entity = _extract_entities(event)
    case_id = _extract_case_id(event)

    raw_reason = (payment_entity.get("error_reason") or payment_entity.get("error_code") or "").lower()
    decline_code = RAZORPAY_REASON_TO_DECLINE_CODE.get(raw_reason, "generic_decline")
    if raw_reason and raw_reason not in RAZORPAY_REASON_TO_DECLINE_CODE:
        logger.warning(
            "case_id=%s: unmapped Razorpay error_reason/code %r -- defaulting decline_code=generic_decline",
            case_id, raw_reason,
        )
    mapper_result = decline_code_mapper.map_razorpay_error_reason(
        raw_reason, error_source=payment_entity.get("error_source")
    )
    decline_code_bucket = mapper_result["decline_code_bucket"]
    decline_code_is_ambiguous = mapper_result["is_ambiguous"]
    if not mapper_result["matched"]:
        logger.info(
            "case_id=%s: error_reason %r not in decline_code_mapper's explicit table -- "
            "defaulted to AMBIGUOUS/is_ambiguous=True",
            case_id, raw_reason,
        )

    amount_paise = payment_entity.get("amount") or subscription_entity.get("amount") or 0
    amount = round(amount_paise / 100.0, 2)

    retry_attempt_number = subscription_entity.get("paid_count")
    if retry_attempt_number is None:
        retry_attempt_number = 1
        logger.info("case_id=%s: no paid_count in payload -- defaulting retry_attempt_number=1", case_id)

    raw_method = (payment_entity.get("method") or "").lower()
    payment_rail = PAYMENT_METHOD_TO_RAIL.get(raw_method, "card")
    if raw_method and raw_method not in PAYMENT_METHOD_TO_RAIL:
        logger.warning(
            "case_id=%s: unmapped payment method %r -- defaulting payment_rail=card", case_id, raw_method
        )

    created_at_epoch = (
        payment_entity.get("created_at") or subscription_entity.get("created_at") or event.get("created_at")
    )
    if created_at_epoch is None:
        created_at_epoch = int(time.time())
    ist_dt = datetime.fromtimestamp(created_at_epoch, tz=IST)
    day_of_week = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][ist_dt.weekday()]
    time_of_day_bucket = _time_of_day_bucket(ist_dt.hour)
    is_peak_execution_window = NPCI_PEAK_START_HOUR <= ist_dt.hour < NPCI_PEAK_END_HOUR

    customer_id = payment_entity.get("customer_id") or subscription_entity.get("customer_id")
    customer_key = _customer_key(customer_id, subscription_entity.get("id"))
    customer_fields, is_first_seen = customer_history.get_or_create_customer(customer_key)
    customer_history_source = "first_seen_defaults" if is_first_seen else "existing_history"
    # Phase 17 -- customer_ptp_stats is keyed by the raw customer_id (the PTP
    # reply-intake identity, not customer_history's customer_key
    # fallback-to-subscription scheme -- see customer_ptp_stats.py's module
    # docstring), so this is looked up separately from customer_fields above.
    # customer_id=None (no Razorpay customer_id on this payload) correctly
    # falls back to "normal" via get_risk_tier's own None-handling.
    current_risk_tier = customer_ptp_stats.get_risk_tier(customer_id)
    if is_first_seen:
        logger.info(
            "case_id=%s: first-seen customer_key=%r -- created new customer_history record with neutral "
            "defaults (customer_tenure_days=0, ltv_tier=medium, historical_ptp_honor_rate=0.5, "
            "prior_retry_success_count=0). This is DIFFERENT from 'we have real history and it happens to "
            "look like this' -- see customer_history_source in the audit row.",
            case_id, customer_key,
        )

    email = payment_entity.get("email")
    contact = payment_entity.get("contact")
    # Phase 18 -- fills the email<->customer_key directory from every webhook
    # call (not just the chat/promise-reply path), so it builds up from real
    # traffic automatically. No-op, logged, if email or customer_key is None
    # -- see customer_directory.record_customer_contact's own docstring.
    customer_directory.record_customer_contact(email, contact, customer_key)

    case = {
        "case_id": case_id,
        "decline_code": decline_code,
        "decline_code_bucket": decline_code_bucket,
        "decline_code_is_ambiguous": decline_code_is_ambiguous,
        "error_description": payment_entity.get("error_description"),
        "error_source": payment_entity.get("error_source"),
        "error_step": payment_entity.get("error_step"),
        "amount": amount,
        "retry_attempt_number": retry_attempt_number,
        "payment_rail": payment_rail,
        "day_of_week": day_of_week,
        "time_of_day_bucket": time_of_day_bucket,
        "is_peak_execution_window": is_peak_execution_window,
        "customer_id": customer_id,
        "customer_key": customer_key,
        "customer_history_source": customer_history_source,
        "current_risk_tier": current_risk_tier,
        "email": email,
        "contact": contact,
        "guardrail_flags": None,
        **customer_fields,
        "hours_since_last_attempt": DEFAULT_HOURS_SINCE_LAST_ATTEMPT,
        "issuer_bank_risk_tier": DEFAULT_ISSUER_BANK_RISK_TIER,
        "amount_vs_historical_avg": DEFAULT_AMOUNT_VS_HISTORICAL_AVG,
    }
    # Same same-transaction proxy shap_extract.get_case_facts uses for the
    # batch (no customer/timestamp linkage across transactions to derive a
    # true 30-day rolling count -- see guardrails.py's own note on this).
    case["cumulative_retries_this_txn"] = case["retry_attempt_number"]
    return case


# --------------------------------------------------------------------------
# Pipeline orchestration -- thin wrappers around pipeline/run_case.py, which
# holds the actual confidence_gate -> shap_extract -> llm_layer -> guardrails
# -> execute_action flow (and its own step-by-step logging). Both of these
# just build the case-shaped dict from a raw webhook event and hand it off;
# /api/trigger-test-case below calls run_case directly with an
# operator-supplied case dict instead of one built from a webhook payload --
# same pipeline functions either way, never duplicated.
# --------------------------------------------------------------------------
def process_recovery_case(event: dict, event_type: str) -> dict:
    case_facts = map_payload_to_case(event)
    return run_case.run_recovery_case(case_facts, event_type, source=run_case.SOURCE_REAL_WEBHOOK)


def process_recovered_case(event: dict, event_type: str) -> dict:
    payment_entity, subscription_entity = _extract_entities(event)
    case_id = _extract_case_id(event)
    amount_paise = payment_entity.get("amount") or subscription_entity.get("amount") or 0
    amount = round(amount_paise / 100.0, 2)
    customer_id = payment_entity.get("customer_id") or subscription_entity.get("customer_id")
    customer_key = _customer_key(customer_id, subscription_entity.get("id"))
    return run_case.run_recovered_case(case_id, amount, customer_key, event_type, source=run_case.SOURCE_REAL_WEBHOOK)


# --------------------------------------------------------------------------
# Flask app
# --------------------------------------------------------------------------
app = Flask(__name__)


@app.before_request
def _log_every_request() -> None:
    """Logs every request this process receives, on any route -- not just
    /webhook/razorpay -- so nothing that hits this server goes unlogged."""
    logger.info(
        "REQUEST %s %s remote_addr=%s content_length=%s user_agent=%s",
        request.method, request.path, request.remote_addr, request.content_length,
        request.headers.get("User-Agent"),
    )


@app.after_request
def _log_every_response(response):
    """Logs the outcome of every request this process handles, matching the
    REQUEST line above by path+method so a request and its response can be
    read as one pair in the log."""
    logger.info("RESPONSE %s %s -> status=%s", request.method, request.path, response.status_code)
    return response


def _log_incoming_webhook_request(raw_body: bytes) -> None:
    """Logs any request Razorpay (or anything else) sends to
    /webhook/razorpay, in full, BEFORE signature verification -- so a
    misconfigured secret, a malformed delivery, or an unexpected sender is
    still fully visible in the log instead of only surfacing as a bare 400.
    Headers are logged in full except Cookie (a webhook delivery has no
    meaningful cookie; excluded on principle, not because one is expected).
    """
    header_summary = {k: v for k, v in request.headers.items() if k.lower() != "cookie"}
    logger.info("Incoming webhook request headers: %s", header_summary)
    try:
        body_text = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        body_text = repr(raw_body)
    logger.info("Incoming webhook request body (%d bytes): %s", len(raw_body), body_text)


@app.route("/webhook/razorpay", methods=["POST"])
def razorpay_webhook():
    raw_body = request.get_data()  # raw bytes -- signature must be checked BEFORE any JSON parsing
    _log_incoming_webhook_request(raw_body)

    signature = request.headers.get("X-Razorpay-Signature", "")

    if not verify_razorpay_signature(raw_body, signature, RAZORPAY_WEBHOOK_SECRET):
        logger.warning("Signature verification FAILED remote_addr=%s", request.remote_addr)
        _append_webhook_log(None, False, None, OUTCOME_SIGNATURE_REJECTED)
        return jsonify({"status": "error", "message": "invalid signature"}), 400

    logger.info("Signature verification OK remote_addr=%s", request.remote_addr)

    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("Body is not valid JSON after signature verification: %s", exc)
        _append_webhook_log(None, True, None, OUTCOME_ERROR, f"invalid JSON: {exc}")
        return jsonify({"status": "error", "message": "invalid JSON"}), 400

    event_type = event.get("event", "unknown")
    logger.info(
        "Event parsed: event=%s account_id=%s contains=%s created_at=%s",
        event_type, event.get("account_id"), event.get("contains"), event.get("created_at"),
    )

    if event_type in RECOVERY_EVENTS:
        case_id = _extract_case_id(event)
        if event_type == "payment.failed":
            # Phase 16 -- observational only, never blocks or alters the
            # decline-recovery pipeline below. Wrapped defensively so a bug
            # here can never turn a normal payment.failed webhook into a 500.
            try:
                ptp_outcomes.handle_payment_failed(event, event_type)
            except Exception:  # noqa: BLE001
                logger.exception("case_id=%s: ptp_outcomes.handle_payment_failed raised", case_id)
        try:
            audit_row = process_recovery_case(event, event_type)
            _append_webhook_log(event_type, True, audit_row["case_id"], OUTCOME_PROCESSED)
            return (
                jsonify(
                    {
                        "status": "processed",
                        "case_id": audit_row["case_id"],
                        "event": event_type,
                        "routed_to_llm": bool(audit_row["routed_to_llm"]),
                        "final_action": audit_row["final_action"],
                        "requires_human_review": bool(audit_row["requires_human_review"]),
                        "guardrail_flags": audit_row["guardrail_flags"],
                    }
                ),
                200,
            )
        except Exception as exc:
            # A pipeline bug must never cause Razorpay to retry-storm us --
            # always ack 200, but flag the case for a human to look at.
            logger.exception("Pipeline error processing case_id=%s event=%s", case_id, event_type)
            error_row = run_case.build_error_row(case_id, event_type, source=run_case.SOURCE_REAL_WEBHOOK)
            run_case.append_webhook_audit_row(error_row)
            _append_webhook_log(event_type, True, case_id, OUTCOME_ERROR, f"{type(exc).__name__}: {exc}")
            return (
                jsonify(
                    {
                        "status": "error",
                        "case_id": case_id,
                        "event": event_type,
                        "requires_human_review": True,
                        "message": "pipeline error -- flagged for human review",
                    }
                ),
                200,
            )

    elif event_type == RECOVERED_EVENT:
        row = process_recovered_case(event, event_type)
        _append_webhook_log(event_type, True, row["case_id"], OUTCOME_PROCESSED)
        return jsonify({"status": "recovered", "case_id": row["case_id"], "event": event_type, "amount": row["amount"]}), 200

    elif event_type in PTP_HONOR_EVENTS:
        try:
            result = ptp_outcomes.handle_payment_captured(event, event_type)
        except Exception as exc:
            logger.exception("PTP honor-tracking error processing event=%s", event_type)
            _append_webhook_log(event_type, True, None, OUTCOME_ERROR, f"{type(exc).__name__}: {exc}")
            return jsonify({"status": "error", "event": event_type, "message": "ptp_outcomes error"}), 200

        outcome = OUTCOME_PROCESSED if result["matched"] else OUTCOME_IGNORED
        _append_webhook_log(event_type, True, result.get("case_id"), outcome)
        return (
            jsonify(
                {
                    "status": "processed" if result["matched"] else "ignored",
                    "event": event_type,
                    "matched_promise": result["matched"],
                    "promise_id": result.get("promise_id"),
                    "case_id": result.get("case_id"),
                    "transition": result.get("transition"),
                }
            ),
            200,
        )

    else:
        logger.info("Event=%s -- no handler, acking as ignored", event_type)
        _append_webhook_log(event_type, True, _extract_case_id(event), OUTCOME_IGNORED)
        return jsonify({"status": "ignored", "event": event_type}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


# --------------------------------------------------------------------------
# Task -- Phase 11 promise-to-pay reply intake: RECEIVES AND STORES the
# customer's raw reply, then runs it through llm_layer.extract_promise_date
# to parse a structured commitment date. promise_store.create_promise is
# called before any other processing so the raw message is durably saved
# even if extraction fails on it -- extraction failure degrades to
# ambiguous=True (see llm_layer.extract_promise_date), it never loses the
# stored reply or 500s the request. Guardrail validation and payment-link
# creation are still later phases, not implemented here.
# --------------------------------------------------------------------------
PROMISE_LOG_PATH = LOGS_DIR / "promise_log.csv"
PROMISE_LOG_COLUMNS = [
    "timestamp", "case_id", "customer_id", "promise_id", "message", "outcome",
    "extracted_date", "extraction_confidence", "ambiguous", "clarification_needed", "model_version",
    # Phase 15 -- clarification-loop state, logged on every promise-related
    # row (not just the final outcome) so a reviewer can read the full
    # ask-again-or-give-up history for a case_id from this one file.
    "clarification_round", "status", "fallback_mechanism",
]


def _append_promise_log(
    case_id: str,
    customer_id: str,
    promise_id: str,
    message: str,
    outcome: str,
    extraction: dict | None = None,
    clarification_round: int = 0,
    status: str | None = None,
    fallback_mechanism: str | None = None,
) -> None:
    PROMISE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    extraction = extraction or {}
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "case_id": case_id,
        "customer_id": customer_id,
        "promise_id": promise_id,
        "message": message,
        "outcome": outcome,
        "extracted_date": extraction.get("extracted_date"),
        "extraction_confidence": extraction.get("confidence"),
        "ambiguous": extraction.get("ambiguous"),
        "clarification_needed": extraction.get("clarification_needed"),
        "model_version": extraction.get("model_version"),
        "clarification_round": clarification_round,
        "status": status,
        "fallback_mechanism": fallback_mechanism,
    }
    # PROMISE_LOG_COLUMNS grew five extraction columns beyond the pre-existing
    # 6-column file this repo already shipped -- reindex any rows written
    # under the old header onto the new one (missing extraction fields come
    # back blank, not guessed) before appending, so the file never ends up
    # with a header row narrower than the data rows under it.
    if PROMISE_LOG_PATH.exists():
        existing = pd.read_csv(PROMISE_LOG_PATH)
        if list(existing.columns) != PROMISE_LOG_COLUMNS:
            existing.reindex(columns=PROMISE_LOG_COLUMNS).to_csv(PROMISE_LOG_PATH, index=False)

    df = pd.DataFrame([row], columns=PROMISE_LOG_COLUMNS)
    write_header = not PROMISE_LOG_PATH.exists()
    df.to_csv(PROMISE_LOG_PATH, mode="a", header=write_header, index=False)


def _promise_date_case_context(case_id: str) -> dict:
    """today is IST-local (same convention map_payload_to_case uses for
    day_of_week/time_of_day_bucket -- see IST above), so "next Friday"
    resolves against the customer's own calendar day, not the server's UTC
    one. amount/decline_code are best-effort grounding context pulled from
    this case's own webhook_audit_log.csv row when one exists -- optional,
    per llm_layer.extract_promise_date's case_context contract, so a lookup
    miss (case not yet in the log, or the log not existing yet) must never
    block extraction."""
    context = {"today": datetime.now(IST).date().isoformat(), "case_id": case_id}
    try:
        if run_case.WEBHOOK_AUDIT_LOG_PATH.exists():
            df = pd.read_csv(run_case.WEBHOOK_AUDIT_LOG_PATH)
            match = df[df["case_id"] == case_id]
            if not match.empty:
                last_row = match.iloc[-1]
                amount = last_row.get("amount")
                decline_code = last_row.get("decline_code")
                if pd.notna(amount):
                    context["amount"] = amount
                if pd.notna(decline_code):
                    context["decline_code"] = decline_code
    except Exception:  # noqa: BLE001 -- grounding context is best-effort, never fatal to extraction
        logger.exception("case_id=%s: failed to load audit-log grounding context for date extraction", case_id)
    return context


# --------------------------------------------------------------------------
# Phase 15 -- clarification loop. When Phase 10's extraction is ambiguous or
# below guardrails.PTP_CONFIDENCE_FLOOR, don't guess and don't run PTP
# guardrails at all -- ask the customer to clarify instead, capped at
# promise_store.MAX_CLARIFICATION_ROUNDS rounds, then fall back automatically.
#
# Each customer reply gets its own fresh promise row (promise_store.
# create_promise runs before extraction on every /api/promise-reply call --
# see the Phase 11 docstring above), so the running clarification_round for
# a case_id is read off the PRIOR row for that case (get_latest_promise_for_case),
# never off this reply's own row, which always starts at clarification_round=0.
# --------------------------------------------------------------------------
DEFAULT_CLARIFICATION_FOLLOW_UP = "Could you give me a specific date, like 'the 5th' or 'next Friday'?"


def _resolve_fallback_schedule(case_id: str) -> tuple[str, str]:
    """Phase 15 fallback scheduling, used once the clarification cap is hit.

    Tries a pattern-based retry-time predictor module first -- none exists
    anywhere under pipeline/ as of this phase (searched for
    retry_time_predictor.py or similar; see pipeline/customer_history.py's
    own note that no per-customer day/time pattern store exists either) --
    and only degrades to a fixed 24-hours-from-now default when no such
    module is importable. Returns (scheduled_for_iso, fallback_mechanism);
    the mechanism string is "predictor" or "fixed_default_24h" so the audit
    trail never conflates a real prediction with the hardcoded default, per
    Phase 15's brief.
    """
    try:
        import retry_time_predictor  # type: ignore  # noqa: F401 -- optional module, may not exist
    except ImportError:
        scheduled_for = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        return scheduled_for, "fixed_default_24h"

    scheduled_for = retry_time_predictor.predict_retry_time(case_id)
    return scheduled_for, "predictor"


def _handle_promise_clarification(case_id: str, promise_id: str, extraction: dict) -> dict:
    """Runs the clarification-loop branch for one ambiguous/low-confidence
    reply. Never calls guardrails.apply_ptp_guardrails -- an unresolved
    extraction has nothing for the PTP rules to check yet.

    Returns a dict always containing status/clarification_round; also
    follow_up_message when status is 'clarifying', or fallback_mechanism +
    fallback (a {scheduled_for, fallback_mechanism} dict) when status is
    'fallback'.
    """
    prior = promise_store.get_latest_promise_for_case(case_id, exclude_promise_id=promise_id)
    previous_round = prior["clarification_round"] if prior else 0

    if previous_round < promise_store.MAX_CLARIFICATION_ROUNDS:
        new_round = previous_round + 1
        promise_store.update_promise_clarification(promise_id, new_round, promise_store.STATUS_CLARIFYING)
        follow_up_message = extraction.get("clarification_needed") or DEFAULT_CLARIFICATION_FOLLOW_UP
        logger.info(
            "case_id=%s promise_id=%s: clarification round %d/%d -- asking follow-up: %r",
            case_id, promise_id, new_round, promise_store.MAX_CLARIFICATION_ROUNDS, follow_up_message,
        )
        return {
            "status": promise_store.STATUS_CLARIFYING,
            "clarification_round": new_round,
            "follow_up_message": follow_up_message,
        }

    # Cap already reached on a prior reply -- stop asking, fall back
    # automatically instead. clarification_round is pinned at the cap, never
    # incremented further, so it cannot exceed MAX_CLARIFICATION_ROUNDS
    # regardless of how many more vague replies arrive for this case_id.
    scheduled_for, fallback_mechanism = _resolve_fallback_schedule(case_id)
    promise_store.update_promise_clarification(
        promise_id, promise_store.MAX_CLARIFICATION_ROUNDS, promise_store.STATUS_FALLBACK
    )
    promise_store.mark_promise_no_reply(promise_id)
    logger.info(
        "case_id=%s promise_id=%s: clarification cap (%d) reached -- falling back via %s scheduled_for=%s",
        case_id, promise_id, promise_store.MAX_CLARIFICATION_ROUNDS, fallback_mechanism, scheduled_for,
    )
    return {
        "status": promise_store.STATUS_FALLBACK,
        "clarification_round": promise_store.MAX_CLARIFICATION_ROUNDS,
        "fallback_mechanism": fallback_mechanism,
        "fallback": {"scheduled_for": scheduled_for, "fallback_mechanism": fallback_mechanism},
    }


def _case_facts_for_ptp_gate(case_id: str) -> dict | None:
    """Reconstructs the subset of the original case dict pipeline/ptp_trigger.
    should_offer_ptp() needs, read back from case_id's own row in
    logs/webhook_audit_log.csv -- the ORIGINAL scoring row specifically
    (.iloc[0], the first row ever written for this case_id), never a later
    reschedule-audit or PTP-rejection row (run_promise_reschedule and
    _append_ptp_rejection_audit_row below both leave these fields blank,
    same as every other run_recovery_case-only column).

    Returns None if case_id has no audit row at all (e.g. the log was reset,
    or a case_id from before this field was added) -- callers degrade
    gracefully rather than blocking the reply on a lookup miss, same
    philosophy as _promise_date_case_context's own best-effort lookup.
    """
    if not run_case.WEBHOOK_AUDIT_LOG_PATH.exists():
        return None
    df = pd.read_csv(run_case.WEBHOOK_AUDIT_LOG_PATH)
    match = df[df["case_id"] == case_id]
    if match.empty:
        return None
    row = match.iloc[0]
    fields = {}
    for col in ("decline_code", "decline_code_bucket", "decline_code_is_ambiguous",
                "retry_attempt_number", "ltv_tier", "payment_rail", "cumulative_retries_this_txn"):
        value = row.get(col)
        fields[col] = value if pd.notna(value) else None
    return fields


def _append_ptp_rejection_audit_row(case_id: str, customer_id: str, eligibility: dict) -> None:
    """Logs a rejected PTP reply attempt to logs/webhook_audit_log.csv --
    per the brief, a reply the pipeline declined to act on is still an
    event worth an audit row, not just a return value. Reuses run_case's
    own blank-row/append helpers rather than a second CSV-append
    mechanism."""
    now_iso = datetime.now(timezone.utc).isoformat()
    row = run_case.blank_webhook_audit_row()
    row.update(
        {
            "case_id": case_id,
            "timestamp": now_iso,
            "routing_rationale": f"PTP reply rejected before LLM extraction -- {eligibility['reason']}",
            "guardrail_flags": "",
            "proposed_action": "ptp_reply_rejected",
            "final_action": "ptp_reply_rejected",
            "guardrail_overrode": False,
            "requires_human_review": False,
            "pipeline_version": run_batch.PIPELINE_VERSION,
            "customer_key": customer_id,
            "execution_status": "not_applicable",
            "execution_mechanism": "none",
            "execution_detail": f"Reply not processed: {eligibility['reason']}",
            "execution_timestamp": now_iso,
            "source": run_case.SOURCE_REAL_WEBHOOK,
            "ptp_offer_decision": eligibility["offer_ptp"],
            "ptp_trigger_category": eligibility["trigger_category"],
            "ptp_offer_reason": eligibility["reason"],
        }
    )
    run_case.append_webhook_audit_row(row)


@app.route("/api/promise-reply", methods=["POST"])
def api_promise_reply():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "bad_request", "message": "expected a JSON object body"}), 400

    case_id = body.get("case_id")
    customer_id = body.get("customer_id")
    message = body.get("message")

    if not isinstance(case_id, str) or not case_id.strip():
        return jsonify({"error": "bad_request", "message": "'case_id' is required and must be non-empty"}), 400
    if not isinstance(customer_id, str) or not customer_id.strip():
        return jsonify({"error": "bad_request", "message": "'customer_id' is required and must be non-empty"}), 400
    if not isinstance(message, str) or not message.strip():
        return jsonify({"error": "bad_request", "message": "'message' is required and must be non-empty"}), 400

    # PTP offer-eligibility gate -- only for this case_id's FIRST reply, not
    # every round of an already-underway clarification loop. Phase 15's
    # clarification loop deliberately re-asks the SAME case up to
    # MAX_CLARIFICATION_ROUNDS times, and each round's own prior reply is a
    # promise_store row still sitting at outcome='pending'/'ambiguous' (i.e.
    # still "open") -- has_open_promise (inside should_offer_ptp) can't tell
    # that apart from a genuinely different, unrelated open promise, so
    # running the gate on round 2/3 would reject a case's own continuing
    # conversation as "open_promise_exists" against itself. Skipping the
    # gate once a case's latest promise is STATUS_CLARIFYING is safe because
    # should_offer_ptp already ran (and passed) on round 1 for this same
    # case_id -- nothing about the case's eligibility changes mid-loop.
    prior_promise = promise_store.get_latest_promise_for_case(case_id)
    is_clarification_continuation = (
        prior_promise is not None and prior_promise.get("status") == promise_store.STATUS_CLARIFYING
    )

    if not is_clarification_continuation:
        # Checked BEFORE promise_store.create_promise() below, for two
        # reasons: (1) has_open_promise (inside should_offer_ptp) must see
        # the state as it was BEFORE this reply, not after -- checking after
        # create_promise would make every reply see its own just-inserted
        # row and always report "open promise exists"; (2) no point
        # extracting a date via the LLM for a promise we're going to reject
        # anyway. Reuses the exact case facts should_offer_ptp saw at
        # scoring time (see _case_facts_for_ptp_gate) rather than re-deriving
        # them differently -- only has_open_promise/current_risk_tier are
        # re-checked live inside should_offer_ptp itself, since those can
        # genuinely change between scoring time and reply time.
        original_case_facts = _case_facts_for_ptp_gate(case_id)
        if original_case_facts is None:
            # No audit row for this case_id (log reset, or a reply arriving
            # for a case never run through the scoring pipeline --
            # deliberately how this repo's own reply-processing test suites
            # exercise /api/promise-reply in isolation, e.g.
            # test_promise_clarification.py). should_offer_ptp() has no
            # decline/retry data to work with here, so its OWN default
            # (retry_attempt_number absent -> 1) would read as a genuine
            # first failure and reject with first_failure_awaiting_auto_
            # retry -- a false rejection manufactured from missing data, not
            # a real fact about this case. retry_attempt_number=2 sidesteps
            # that specific default without touching should_offer_ptp()
            # itself: it does NOT bypass has_open_promise/current_risk_tier
            # (both still run first, unconditionally, and both are genuinely
            # known regardless of whether a decline was ever logged for this
            # case_id) -- it only keeps an unrelated absence of data from
            # masquerading as "this is definitely a first failure."
            logger.info(
                "case_id=%s: no audit row found for PTP gate -- proceeding without decline/retry "
                "context (open-promise/risk-tier checks still apply)",
                case_id,
            )
            gate_case = {"customer_id": customer_id, "retry_attempt_number": 2}
        else:
            gate_case = dict(original_case_facts)
            gate_case["customer_id"] = customer_id

        eligibility = ptp_trigger.should_offer_ptp(gate_case)
        if not eligibility["offer_ptp"]:
            logger.info(
                "case_id=%s customer_id=%s: PTP reply REJECTED -- trigger_category=%s reason=%s",
                case_id, customer_id, eligibility["trigger_category"], eligibility["reason"],
            )
            _append_ptp_rejection_audit_row(case_id, customer_id, eligibility)
            return (
                jsonify(
                    {
                        "status": "rejected",
                        "case_id": case_id,
                        "ptp_trigger_category": eligibility["trigger_category"],
                        "ptp_offer_reason": eligibility["reason"],
                        "message": "This case is not eligible for a promise-to-pay reply right now.",
                    }
                ),
                200,
            )

    # Insert happens before anything else -- the raw reply must be durably
    # saved even if later processing (date extraction) fails on it.
    promise_id = promise_store.create_promise(case_id, customer_id, message)
    logger.info(
        "Promise reply received: promise_id=%s case_id=%s customer_id=%s remote_addr=%s",
        promise_id, case_id, customer_id, request.remote_addr,
    )

    case_context = _promise_date_case_context(case_id)
    extraction = llm_layer.extract_promise_date(message, case_context)
    logger.info(
        "Promise date extraction: promise_id=%s case_id=%s extracted_date=%s confidence=%s ambiguous=%s "
        "model_version=%s",
        promise_id, case_id, extraction["extracted_date"], extraction["confidence"], extraction["ambiguous"],
        extraction["model_version"],
    )
    promise_store.update_promise_extraction(
        promise_id, extraction["extracted_date"], extraction["confidence"], extraction["ambiguous"]
    )

    # Phase 15 -- an ambiguous or below-PTP_CONFIDENCE_FLOOR extraction never
    # reaches guardrails at all; it goes through the clarification loop
    # instead (ask again, capped at MAX_CLARIFICATION_ROUNDS, then fall
    # back automatically). Only a clean extraction (a date, confident,
    # unambiguous) proceeds to guardrails -- exactly Phase 11-14's existing
    # behavior, unchanged below.
    is_clean_extraction = (
        extraction["extracted_date"] is not None
        and extraction["confidence"] >= guardrails.PTP_CONFIDENCE_FLOOR
        and not extraction["ambiguous"]
    )

    if not is_clean_extraction:
        clarification_result = _handle_promise_clarification(case_id, promise_id, extraction)
        outcome = (
            promise_store.OUTCOME_NO_REPLY
            if clarification_result["status"] == promise_store.STATUS_FALLBACK
            else promise_store.OUTCOME_AMBIGUOUS
        )
        _append_promise_log(
            case_id, customer_id, promise_id, message, outcome, extraction,
            clarification_round=clarification_result["clarification_round"],
            status=clarification_result["status"],
            fallback_mechanism=clarification_result.get("fallback_mechanism"),
        )
        return (
            jsonify(
                {
                    "promise_id": promise_id,
                    "status": clarification_result["status"],
                    "extracted_date": extraction["extracted_date"],
                    "extraction_confidence": extraction["confidence"],
                    "ambiguous": extraction["ambiguous"],
                    "clarification_needed": extraction["clarification_needed"],
                    "clarification_round": clarification_result["clarification_round"],
                    "follow_up_message": clarification_result.get("follow_up_message"),
                    "fallback": clarification_result.get("fallback"),
                }
            ),
            200,
        )

    outcome = promise_store.OUTCOME_PENDING
    # status starts at 'pending' here and is only ever raised to a more
    # final value below (requires_human_review / scheduled) -- the log row
    # appended after this block reflects whichever one actually applied, not
    # a stale 'pending' snapshot taken before guardrails/execution ran.
    final_status = promise_store.STATUS_PENDING

    # Phase 14 -- run the extraction through the Phase 13 PTP guardrails and,
    # only when they approve it, execute the reschedule (create the Razorpay
    # payment link). guardrail_status is written back onto the promise row
    # regardless of the verdict, same as extracted_date/confidence above --
    # rejected/pending_clarification verdicts route to human review /
    # clarification (both already surfaced via guardrail_result's own
    # requires_human_review / routed_to_clarification flags) rather than
    # calling run_case.run_promise_reschedule at all.
    guardrail_result = guardrails.apply_ptp_guardrails(case_context, extraction)
    promise_store.update_promise_guardrail(promise_id, guardrail_result["guardrail_status"])
    if guardrail_result.get("requires_human_review"):
        final_status = promise_store.STATUS_REQUIRES_HUMAN_REVIEW
        promise_store.update_promise_status(promise_id, final_status)
    logger.info(
        "Promise guardrail verdict: promise_id=%s case_id=%s guardrail_status=%s final_action=%s "
        "guardrail_flags=%s requires_human_review=%s routed_to_clarification=%s",
        promise_id, case_id, guardrail_result["guardrail_status"], guardrail_result["final_action"],
        guardrail_result["guardrail_flags"], guardrail_result["requires_human_review"],
        guardrail_result["routed_to_clarification"],
    )

    reschedule_result = None
    if guardrail_result["guardrail_status"] == "approved":
        promise_record = {
            "promise_id": promise_id,
            "case_id": case_id,
            "customer_id": customer_id,
            "extracted_date": extraction["extracted_date"],
        }
        reschedule_row = run_case.run_promise_reschedule(
            case_context, promise_record, guardrail_result, source=run_case.SOURCE_REAL_WEBHOOK
        )
        reschedule_result = {
            "execution_status": reschedule_row["execution_status"],
            "execution_mechanism": reschedule_row["execution_mechanism"],
        }
        if reschedule_row["execution_status"] == "success":
            final_status = promise_store.STATUS_SCHEDULED

    _append_promise_log(
        case_id, customer_id, promise_id, message, outcome, extraction,
        clarification_round=0, status=final_status,
    )

    return (
        jsonify(
            {
                "promise_id": promise_id,
                "status": "received",
                "extracted_date": extraction["extracted_date"],
                "extraction_confidence": extraction["confidence"],
                "ambiguous": extraction["ambiguous"],
                "clarification_needed": extraction["clarification_needed"],
                "guardrail_status": guardrail_result["guardrail_status"],
                "reschedule": reschedule_result,
            }
        ),
        200,
    )


# --------------------------------------------------------------------------
# Task 3 -- crude in-memory rate limit for the trigger endpoint. Global (not
# per-IP) and reset on process restart -- deliberately simple, this only
# needs to stop the endpoint being hammered into generating unbounded audit
# rows or unbounded real Razorpay payment-link creations (execute_action
# calls a real API for retry_now/prompt_alt_payment presets), not survive a
# distributed attack.
# --------------------------------------------------------------------------
TRIGGER_RATE_LIMIT = 20
TRIGGER_RATE_WINDOW_SECONDS = 60
_trigger_request_times: deque = deque()


def _check_trigger_rate_limit() -> bool:
    now = time.time()
    while _trigger_request_times and now - _trigger_request_times[0] > TRIGGER_RATE_WINDOW_SECONDS:
        _trigger_request_times.popleft()
    if len(_trigger_request_times) >= TRIGGER_RATE_LIMIT:
        return False
    _trigger_request_times.append(now)
    return True


# --------------------------------------------------------------------------
# Task 2 -- read-only dashboard API
# --------------------------------------------------------------------------
def _df_records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-safe list of dicts. Uses pandas' own to_json (not
    .to_dict()) because it correctly converts numpy scalar dtypes
    (int64/float64/bool_) and NaN -> null -- .to_dict() leaves numpy scalars
    in place, which Flask's json encoder can't serialize, and
    legitimately-empty cells (e.g. llm_confidence on a non-LLM-routed row)
    are the normal case in this data, not an edge case to special-case
    around."""
    if df.empty:
        return []
    return json.loads(df.to_json(orient="records"))


def _read_log_tail(path: Path, limit: int, offset: int) -> tuple[list[dict], int]:
    if not path.exists():
        return [], 0
    df = pd.read_csv(path)
    total = len(df)
    df = df.iloc[::-1]  # newest first -- the file is append-only in chronological order
    df = df.iloc[offset: offset + limit]
    return _df_records(df), total


def _annotate_ptp_status(rows: list[dict]) -> None:
    """Adds `ptp_status` to each audit-log row in place -- the dashboard's
    Audit Log table wants, per case, either the customer-committed
    scheduled date or an explicit "still waiting" marker, without the
    operator having to open the case-detail modal to find out.

    Reads the case's LATEST promise-to-pay reply only (same "most recent
    reply wins" convention dashboard/conversations.html's renderCaseCard
    already uses for its own outcome banner) -- an earlier reply's outcome
    is superseded once a later one exists. 'Scheduled for: <date>' only
    when that latest reply actually reached STATUS_SCHEDULED; every other
    state (no reply yet, still clarifying, fell back, rejected by a
    guardrail) reads the same to an operator scanning this table -- no
    customer-confirmed date exists yet -- so they all collapse to
    'Waiting for user response' rather than a half-dozen granular labels
    only the case-detail modal needs."""
    case_ids = [r["case_id"] for r in rows if r.get("case_id")]
    latest_promises = promise_store.get_latest_promise_status_for_cases(case_ids)
    for row in rows:
        promise = latest_promises.get(row.get("case_id"))
        if promise and promise.get("status") == promise_store.STATUS_SCHEDULED:
            row["ptp_status"] = f"Scheduled for: {promise.get('extracted_date')}"
        else:
            row["ptp_status"] = "Waiting for user response"


@app.route("/api/audit-log", methods=["GET"])
def api_audit_log():
    """Reads logs/webhook_audit_log.csv -- the live pipeline-decision log
    (real webhook cases AND dashboard-triggered synthetic ones, see `source`
    column). NOT logs/audit_log.csv, which is exclusively
    pipeline/run_batch.py's frozen 280-row synthetic training-batch output
    and is never touched by anything in this file."""
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    rows, total = _read_log_tail(run_case.WEBHOOK_AUDIT_LOG_PATH, limit, offset)
    _annotate_ptp_status(rows)
    return jsonify({"rows": rows, "count": len(rows), "total": total})


@app.route("/api/webhook-log", methods=["GET"])
def api_webhook_log():
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    rows, total = _read_log_tail(WEBHOOK_LOG_PATH, limit, offset)
    return jsonify({"rows": rows, "count": len(rows), "total": total})


def _count_true(series: pd.Series) -> int:
    if series.dtype == bool:
        return int(series.sum())
    return int(series.astype(str).str.strip().str.lower().eq("true").sum())


@app.route("/api/summary", methods=["GET"])
def api_summary():
    path = run_case.WEBHOOK_AUDIT_LOG_PATH
    if not path.exists():
        return jsonify(
            {
                "total_cases": 0, "routed_to_llm": 0, "guardrail_overrode": 0, "requires_human_review": 0,
                "recovered": 0, "failed_or_pending": 0, "execution_status_breakdown": {},
            }
        )

    df = pd.read_csv(path)
    total = len(df)
    recovered = int((df["final_action"] == "recovered").sum()) if "final_action" in df.columns else 0
    execution_status_breakdown = (
        df["execution_status"].fillna("unknown").value_counts().to_dict() if "execution_status" in df.columns else {}
    )

    return jsonify(
        {
            "total_cases": total,
            "routed_to_llm": _count_true(df["routed_to_llm"]) if "routed_to_llm" in df.columns else 0,
            "guardrail_overrode": _count_true(df["guardrail_overrode"]) if "guardrail_overrode" in df.columns else 0,
            "requires_human_review": (
                _count_true(df["requires_human_review"]) if "requires_human_review" in df.columns else 0
            ),
            "recovered": recovered,
            "failed_or_pending": total - recovered,
            "execution_status_breakdown": execution_status_breakdown,
        }
    )


# --------------------------------------------------------------------------
# Task 3 -- synthetic test-case trigger, bypassing real Razorpay entirely.
#
# Body: {"event_type": "payment.failed" (default) | "subscription.pending" |
# "subscription.halted" | "subscription.charged", "case": {...}}. "case" is
# shaped like map_payload_to_case()'s output -- any field the caller omits
# falls back to DEFAULT_TEST_CASE below. Reuses run_case.run_recovery_case /
# run_recovered_case directly -- the exact same functions the real webhook
# path calls above -- nothing pipeline-related is reimplemented here.
#
# Every triggered case_id is forced to start with "manual_test_" and every
# row is written with source="manual_test" (see run_case.py), so it can
# never be mistaken for a real customer event in the audit trail.
# --------------------------------------------------------------------------
MANUAL_TEST_CASE_ID_PREFIX = "manual_test_"

DEFAULT_TEST_CASE = {
    "decline_code": "generic_decline",
    "decline_code_bucket": "AMBIGUOUS",
    "decline_code_is_ambiguous": True,
    "error_description": None,
    "error_source": None,
    "error_step": None,
    "amount": 500.0,
    "retry_attempt_number": 1,
    "payment_rail": "card",
    "day_of_week": "mon",
    "time_of_day_bucket": "morning",
    "is_peak_execution_window": False,
    "customer_id": None,
    "customer_key": None,
    "customer_history_source": "synthetic_test_default",
    "current_risk_tier": "normal",
    "email": None,
    "contact": None,
    "guardrail_flags": None,
    "customer_tenure_days": 365,
    "ltv_tier": "medium",
    "historical_ptp_honor_rate": 0.5,
    "prior_retry_success_count": 0,
    "hours_since_last_attempt": 24.0,
    "issuer_bank_risk_tier": "medium_risk",
    "amount_vs_historical_avg": 1.0,
}


def _default_manual_test_case_id() -> str:
    return f"{MANUAL_TEST_CASE_ID_PREFIX}{int(time.time() * 1000)}"


def _force_manual_test_prefix(case_id: str | None) -> str:
    if not case_id:
        return _default_manual_test_case_id()
    return case_id if str(case_id).startswith(MANUAL_TEST_CASE_ID_PREFIX) else f"{MANUAL_TEST_CASE_ID_PREFIX}{case_id}"


def _build_manual_test_case(payload_case: dict) -> dict:
    case = {**DEFAULT_TEST_CASE, **(payload_case or {})}
    case["case_id"] = _force_manual_test_prefix(case.get("case_id"))
    case.setdefault("cumulative_retries_this_txn", case["retry_attempt_number"])
    # Preset/custom internal-shape triggers bypass map_payload_to_case()
    # entirely (that's the point -- precise guardrail-rule targeting without
    # a full Razorpay envelope), so it never got a chance to populate Phase
    # 18's email<->customer_key directory the way a real webhook or
    # /api/trigger-webhook-shaped does. Same no-op-if-missing behavior as
    # that call: DEFAULT_TEST_CASE's email/customer_key are both None, so
    # this is a no-op for the existing guardrail-targeting presets, and only
    # takes effect for a preset/custom payload that explicitly sets both.
    customer_directory.record_customer_contact(
        case.get("email"), case.get("contact"), case.get("customer_key")
    )
    return case


@app.route("/api/trigger-test-case", methods=["POST"])
def api_trigger_test_case():
    if not _check_trigger_rate_limit():
        return (
            jsonify(
                {
                    "error": "rate_limited",
                    "message": f"Max {TRIGGER_RATE_LIMIT} trigger requests per {TRIGGER_RATE_WINDOW_SECONDS}s",
                }
            ),
            429,
        )

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "bad_request", "message": "expected a JSON object body"}), 400

    event_type = body.get("event_type") or "payment.failed"
    payload_case = body.get("case") or {}
    if not isinstance(payload_case, dict):
        return jsonify({"error": "bad_request", "message": "'case' must be a JSON object"}), 400

    logger.info("Dashboard trigger: event_type=%s remote_addr=%s", event_type, request.remote_addr)

    try:
        if event_type == RECOVERED_EVENT:
            case_id = _force_manual_test_prefix(payload_case.get("case_id"))
            amount = float(payload_case.get("amount", 500.0))
            customer_key = payload_case.get("customer_key")
            row = run_case.run_recovered_case(
                case_id, amount, customer_key, event_type, source=run_case.SOURCE_MANUAL_TEST
            )
        else:
            case_facts = _build_manual_test_case(payload_case)
            row = run_case.run_recovery_case(case_facts, event_type, source=run_case.SOURCE_MANUAL_TEST)

        _append_webhook_log(event_type, True, row["case_id"], OUTCOME_PROCESSED)
        return jsonify({"status": "processed", "row": _df_records(pd.DataFrame([row]))[0]}), 200

    except Exception as exc:
        logger.exception("Dashboard trigger error: event_type=%s", event_type)
        _append_webhook_log(
            event_type, True, payload_case.get("case_id"), OUTCOME_ERROR, f"{type(exc).__name__}: {exc}"
        )
        return jsonify({"status": "error", "message": f"{type(exc).__name__}: {exc}"}), 500


# --------------------------------------------------------------------------
# Phase 21 -- real-Razorpay-shape trigger endpoint. Additive: does NOT modify
# or replace /api/trigger-test-case above (several dashboard presets depend
# on that endpoint's internal-case-shape/DEFAULT_TEST_CASE contract for
# precise guardrail-rule targeting -- this endpoint exercises the OTHER half
# of the pipeline, the raw-payload mapper, instead).
#
# Body: the full Razorpay webhook envelope shape -- {entity, event, payload:
# {payment: {entity: {...}}, subscription: {...}}, created_at} -- same
# structure test_webhook_locally.py's SAMPLE_PAYMENT_FAILED uses. Internally
# calls the exact same map_payload_to_case() + run_case.run_recovery_case()
# path /webhook/razorpay uses for payment.failed/subscription.pending/
# subscription.halted (or run_recovered_case() for subscription.charged) --
# nothing about that mapping is reimplemented or shortcut here.
#
# case_id is forced onto the MANUAL_TEST_CASE_ID_PREFIX convention and every
# row is written with source=SOURCE_MANUAL_TEST, same as
# /api/trigger-test-case, so these can never be mistaken for a real webhook
# row in the audit trail. Shares _check_trigger_rate_limit()'s bucket --
# deliberately not a second independent limiter.
# --------------------------------------------------------------------------
@app.route("/api/trigger-webhook-shaped", methods=["POST"])
def api_trigger_webhook_shaped():
    if not _check_trigger_rate_limit():
        return (
            jsonify(
                {
                    "error": "rate_limited",
                    "message": f"Max {TRIGGER_RATE_LIMIT} trigger requests per {TRIGGER_RATE_WINDOW_SECONDS}s",
                }
            ),
            429,
        )

    event = request.get_json(silent=True)
    if not isinstance(event, dict):
        return jsonify({"error": "bad_request", "message": "expected a JSON object body"}), 400

    event_type = event.get("event") or "payment.failed"
    logger.info("Dashboard trigger (webhook-shaped): event=%s remote_addr=%s", event_type, request.remote_addr)

    try:
        if event_type == RECOVERED_EVENT:
            # Mirrors process_recovered_case's own extraction (same
            # map_payload_to_case-adjacent field reads: amount/customer_id/
            # customer_key), just forcing case_id + source=SOURCE_MANUAL_TEST
            # instead of process_recovered_case's SOURCE_REAL_WEBHOOK.
            payment_entity, subscription_entity = _extract_entities(event)
            amount_paise = payment_entity.get("amount") or subscription_entity.get("amount") or 0
            amount = round(amount_paise / 100.0, 2)
            customer_id = payment_entity.get("customer_id") or subscription_entity.get("customer_id")
            customer_key = _customer_key(customer_id, subscription_entity.get("id"))
            case_id = _force_manual_test_prefix(_extract_case_id(event))
            row = run_case.run_recovered_case(
                case_id, amount, customer_key, event_type, source=run_case.SOURCE_MANUAL_TEST
            )
        else:
            case_facts = map_payload_to_case(event)
            case_facts["case_id"] = _force_manual_test_prefix(case_facts["case_id"])
            row = run_case.run_recovery_case(case_facts, event_type, source=run_case.SOURCE_MANUAL_TEST)

        _append_webhook_log(event_type, True, row["case_id"], OUTCOME_PROCESSED)
        return jsonify({"status": "processed", "row": _df_records(pd.DataFrame([row]))[0]}), 200

    except Exception as exc:
        logger.exception("Dashboard trigger (webhook-shaped) error: event=%s", event_type)
        _append_webhook_log(event_type, True, None, OUTCOME_ERROR, f"{type(exc).__name__}: {exc}")
        return jsonify({"status": "error", "message": f"{type(exc).__name__}: {exc}"}), 500


# --------------------------------------------------------------------------
# Task -- per-case detail view: the full flow for ONE case_id in one place --
# its audit row (SHAP top features, routing_rationale, LLM reasoning_summary,
# guardrail flags/override, execution result, final_action), its
# webhook_log.csv transport-layer row(s) if any, and every matching line
# from logs/webhook_receiver.log's step-by-step trace (see
# pipeline/run_case.py's "step N/6" logging) -- so "what happened to this
# case, in order, including what the LLM actually said" is answerable from
# one screen instead of cross-referencing three files by hand.
# --------------------------------------------------------------------------
MAX_LOG_LINES_RETURNED = 500


@app.route("/api/case-detail/<path:case_id>", methods=["GET"])
def api_case_detail(case_id):
    audit_row = None
    if run_case.WEBHOOK_AUDIT_LOG_PATH.exists():
        df = pd.read_csv(run_case.WEBHOOK_AUDIT_LOG_PATH)
        match = df[df["case_id"] == case_id]
        if not match.empty:
            audit_row = _df_records(match)[0]

    webhook_log_rows = []
    if WEBHOOK_LOG_PATH.exists():
        wdf = pd.read_csv(WEBHOOK_LOG_PATH)
        wmatch = wdf[wdf["case_id"] == case_id]
        webhook_log_rows = _df_records(wmatch)

    log_lines = []
    receiver_log_path = LOGS_DIR / "webhook_receiver.log"
    if receiver_log_path.exists():
        needle = f"case_id={case_id}"
        with open(receiver_log_path, "r", encoding="utf-8", errors="replace") as f:
            log_lines = [line.rstrip("\n") for line in f if needle in line]
        if len(log_lines) > MAX_LOG_LINES_RETURNED:
            log_lines = log_lines[-MAX_LOG_LINES_RETURNED:]

    # Phase 19's promises table is the persisted PTP reply history for this
    # case (see get_promises_for_case's docstring for why that's `promises`,
    # not logs/promise_log.csv) -- included here so the case-detail modal's
    # "Reply as customer" section can show whether/when this case is
    # actually scheduled, not just the one-off status text from the moment a
    # reply was last sent (which disappears the instant the modal reopens).
    # Shaped via _promise_thread_entry (same as /api/customer/<email>/
    # conversations) so the frontend gets `message`/`scheduled_outcome`
    # consistently instead of promise_store's raw `raw_customer_reply` column.
    promises = [_promise_thread_entry(p) for p in promise_store.get_promises_for_case(case_id)]

    if audit_row is None and not webhook_log_rows and not log_lines and not promises:
        return jsonify({"error": "not_found", "message": f"No data found for case_id={case_id!r}"}), 404

    return jsonify(
        {
            "case_id": case_id,
            "audit_row": audit_row,
            "webhook_log_rows": webhook_log_rows,
            "log_lines": log_lines,
            "promises": promises,
        }
    )


# --------------------------------------------------------------------------
# Phase 19 -- conversation-history read API. Read-only: does not modify
# api_promise_reply or any other existing write path.
#
# Joins Phase 18's customer_directory (email <-> customer_key) against
# logs/webhook_audit_log.csv's own customer_key column -- the only place
# case_id <-> customer_key is recorded -- to find every case_id a given
# email has ever had, then reads each case's full reply thread from
# pipeline/promise_store.py's `promises` table (the source of truth for a
# promise's lifecycle state; see get_promises_for_case's docstring for why
# that's `promises`, not logs/promise_log.csv, which lacks guardrail_status/
# payment_link_id entirely).
#
# <path:email> mirrors api_case_detail's <path:case_id> above -- Werkzeug's
# path converter URL-decodes the segment for us, so an email arriving as
# `foo%40example.com` or literal `foo@example.com` both resolve correctly.
# --------------------------------------------------------------------------
def _load_audit_log_df() -> pd.DataFrame | None:
    if not run_case.WEBHOOK_AUDIT_LOG_PATH.exists():
        return None
    return pd.read_csv(run_case.WEBHOOK_AUDIT_LOG_PATH)


def _is_conversation_worthy_case(df: pd.DataFrame, case_id: str) -> bool:
    """False only for a hard-decline case with no reply history at all --
    should_offer_ptp's hard_decline verdict means "the payment method
    itself won't work, so a date commitment is meaningless" (see
    pipeline/ptp_trigger.py), a PERMANENT fact about the case, not a
    conditional/temporary one -- unlike open_promise_exists,
    restricted_tier, or first_failure_awaiting_auto_retry, which can all
    still have (or later gain) real conversation activity worth showing.
    Listing a case that can never have a PTP conversation on the Customer
    Conversations page invites an operator to reply to it, only for
    api_promise_reply's own should_offer_ptp gate to reject it.

    Looks up the case's FIRST audit row (the original scoring row -- see
    api_customer_conversations' own case_summary lookup for why not the
    latest) for ptp_trigger_category. A hard-decline case that DOES already
    have a promise/reply thread (e.g. from before this gate existed) still
    shows -- there's real history there worth seeing regardless of what a
    fresh reply would now be rejected for."""
    match = df[df["case_id"] == case_id]
    if match.empty:
        return True
    trigger_category = match.iloc[0].get("ptp_trigger_category")
    if trigger_category != ptp_trigger.CATEGORY_HARD_DECLINE:
        return True
    return bool(promise_store.get_promises_for_case(case_id))


def _case_ids_and_activity_for_customer_keys(
    df: pd.DataFrame | None, customer_keys: list[str]
) -> tuple[list[str], str | None]:
    """Unique case_ids and the latest timestamp across every
    webhook_audit_log.csv row whose customer_key is one of customer_keys --
    excluding hard-decline cases with no reply thread (see
    _is_conversation_worthy_case). Returns ([], None) if there's no audit
    log yet or no matching rows -- a known email with no audit-log activity
    yet degrades to an empty case list, not an error."""
    if df is None or df.empty or not customer_keys:
        return [], None
    match = df[df["customer_key"].isin(customer_keys)]
    if match.empty:
        return [], None
    case_ids = sorted(match["case_id"].dropna().unique().tolist())
    case_ids = [cid for cid in case_ids if _is_conversation_worthy_case(df, cid)]
    if not case_ids:
        return [], None
    match = match[match["case_id"].isin(case_ids)]
    last_activity = match["timestamp"].dropna().max() if "timestamp" in match.columns else None
    return case_ids, (last_activity if pd.notna(last_activity) else None)


@app.route("/api/customers", methods=["GET"])
def api_customers():
    df = _load_audit_log_df()
    directory_rows = customer_directory.list_all_customers()

    results = []
    for row in directory_rows:
        case_ids, last_activity = _case_ids_and_activity_for_customer_keys(df, row["customer_keys"])
        # A customer whose only case(s) are hard-decline with no reply
        # thread (see _is_conversation_worthy_case) has nothing to show on
        # this page at all -- listing them with "0 cases" is exactly as
        # misleading as listing the hard-decline case itself would be.
        if not case_ids:
            continue
        results.append(
            {
                "email": row["email"],
                "customer_keys": row["customer_keys"],
                # Included (not just case_count) so the Customer Conversations
                # page's search box can match a pasted case_id and find the
                # owning customer, not just an email substring.
                "case_ids": case_ids,
                "case_count": len(case_ids),
                # Falls back to the directory's own last_seen_at (set on every
                # webhook, even ones that never reach the audit log e.g. a
                # signature rejection never happens here since this is post-
                # verification, but this keeps a freshly-seen customer with
                # zero audit rows from sorting as if never active).
                "last_activity": last_activity or row["last_seen_at"],
            }
        )

    results.sort(key=lambda r: r["last_activity"] or "", reverse=True)
    return jsonify({"customers": results, "count": len(results)})


def _promise_thread_entry(promise: dict) -> dict:
    """One promises-table row shaped for the conversations UI -- every field
    Phase 19's brief asks for, plus a derived `scheduled_outcome` summary so
    the frontend doesn't need to re-derive STATUS_SCHEDULED/STATUS_FALLBACK
    branching itself."""
    status = promise.get("status")
    outcome = promise.get("outcome")
    if status == promise_store.STATUS_SCHEDULED:
        scheduled_outcome = {"kind": "scheduled", "scheduled_for": promise.get("extracted_date")}
    elif status == promise_store.STATUS_FALLBACK:
        scheduled_outcome = {"kind": "fallback", "scheduled_for": promise.get("extracted_date")}
    elif outcome == promise_store.OUTCOME_RESCHEDULE_FAILED:
        # Guardrail APPROVED this reply (status never left STATUS_PENDING --
        # only a successful execution moves it to STATUS_SCHEDULED, see
        # run_case.run_promise_reschedule) but the actual Razorpay API call
        # to create the payment link failed. That's a materially different
        # fact from "no reply yet" -- collapsing it into 'awaiting_reply'
        # would hide a real execution failure behind a generic waiting state.
        scheduled_outcome = {"kind": "reschedule_failed", "extracted_date": promise.get("extracted_date")}
    elif status in (promise_store.STATUS_PENDING, promise_store.STATUS_CLARIFYING):
        scheduled_outcome = {"kind": "awaiting_reply"}
    elif status == promise_store.STATUS_REQUIRES_HUMAN_REVIEW:
        scheduled_outcome = {"kind": "requires_human_review"}
    else:
        scheduled_outcome = {"kind": status}

    return {
        "promise_id": promise.get("promise_id"),
        "message": promise.get("raw_customer_reply"),
        "created_at": promise.get("created_at"),
        "extracted_date": promise.get("extracted_date"),
        "extraction_confidence": promise.get("extraction_confidence"),
        "guardrail_status": promise.get("guardrail_status"),
        "clarification_round": promise.get("clarification_round"),
        "status": status,
        "outcome": promise.get("outcome"),
        "payment_link_id": promise.get("payment_link_id"),
        "scheduled_outcome": scheduled_outcome,
    }


@app.route("/api/customer/<path:email>/conversations", methods=["GET"])
def api_customer_conversations(email):
    customer_keys = customer_directory.get_customer_keys_for_email(email)
    if not customer_keys:
        return jsonify({"error": "not_found", "message": f"No known customer for email={email!r}"}), 404

    df = _load_audit_log_df()
    case_ids, _ = _case_ids_and_activity_for_customer_keys(df, customer_keys)

    cases = []
    for case_id in case_ids:
        case_summary = None
        if df is not None:
            match = df[df["case_id"] == case_id]
            if not match.empty:
                # First row = the original payment.failed/subscription.*
                # audit row (run_promise_reschedule appends later rows under
                # the SAME case_id for each approved reply -- see that
                # function's docstring -- so amount/decline_code context
                # comes from the earliest row, not whichever happens last).
                case_summary = _df_records(match.iloc[[0]])[0]

        promises = [_promise_thread_entry(p) for p in promise_store.get_promises_for_case(case_id)]
        cases.append({"case_id": case_id, "case_summary": case_summary, "promises": promises})

    return jsonify({"email": email, "customer_keys": customer_keys, "cases": cases, "case_count": len(cases)})


# --------------------------------------------------------------------------
# Phase 16 -- manual/cron trigger for the deadline sweep, alongside the
# background thread started in _startup_checks(). Lets an operator (or an
# external cron, or a demo) force the check_expired_promises() pass on
# demand instead of waiting for the next timer tick.
# --------------------------------------------------------------------------
@app.route("/api/ptp/check-expired", methods=["POST"])
def api_ptp_check_expired():
    results = ptp_outcomes.check_expired_promises()
    return jsonify({"status": "ok", "broken_count": len(results), "broken": results})


# --------------------------------------------------------------------------
# Dashboard "reset" -- clears the demo/log state back to empty so a session
# of preset-clicking and custom triggers doesn't linger into the next demo.
# Empties (not deletes) the three CSV logs by rewriting them header-only, so
# /api/audit-log etc. keep working immediately (an empty list, not a missing
# file). logs/audit_log.csv (the synthetic training batch) and
# logs/webhook_receiver.log (the live text trace of the running process,
# which Windows will refuse to truncate out from under an open file handle
# anyway) are deliberately never touched here.
#
# reset_customer_history is opt-in (default False) -- it wipes real
# accumulated per-customer state (pipeline/customer_history.py), which is
# a bigger, less obviously-reversible action than clearing a log, so it
# isn't bundled into the default reset.
# --------------------------------------------------------------------------
@app.route("/api/reset-logs", methods=["POST"])
def api_reset_logs():
    body = request.get_json(silent=True) or {}
    reset_customer_history = bool(body.get("reset_customer_history", False))

    reset = []

    run_case.WEBHOOK_AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(columns=run_case.WEBHOOK_AUDIT_COLUMNS).to_csv(run_case.WEBHOOK_AUDIT_LOG_PATH, index=False)
    reset.append(run_case.WEBHOOK_AUDIT_LOG_PATH.name)

    WEBHOOK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(columns=WEBHOOK_LOG_COLUMNS).to_csv(WEBHOOK_LOG_PATH, index=False)
    reset.append(WEBHOOK_LOG_PATH.name)

    execute_action.PENDING_RETRIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(columns=execute_action.PENDING_RETRIES_COLUMNS).to_csv(execute_action.PENDING_RETRIES_PATH, index=False)
    reset.append(execute_action.PENDING_RETRIES_PATH.name)

    if reset_customer_history and customer_history.DB_PATH.exists():
        customer_history.DB_PATH.unlink()
        reset.append(customer_history.DB_PATH.name)

    logger.warning(
        "Dashboard reset triggered: cleared %s (reset_customer_history=%s) remote_addr=%s",
        reset, reset_customer_history, request.remote_addr,
    )
    return jsonify({"status": "reset", "cleared": reset, "reset_customer_history": reset_customer_history})


# --------------------------------------------------------------------------
# Phase 20 follow-up -- "reset" for the Customer Conversations page. Scoped
# to exactly what that page reads: Phase 18's customer_directory (email <->
# customer_key) and the promises table (promise-reply thread state).
# Deliberately does NOT touch customer_history's tenure/ltv/honor-rate
# scoring table -- same database file, but that's pipeline scoring state,
# not conversation data -- and does NOT touch webhook_audit_log.csv, since
# /api/customer/<email>/conversations still needs it to resolve which
# case_ids a customer has, and the main dashboard's own tables read the same
# file. (Note: /api/reset-logs's reset_customer_history=true option deletes
# the whole database file wholesale, which as a side effect also wipes these
# same two tables -- that's the "bigger, less obviously-reversible" reset;
# this one is the narrower, page-scoped equivalent.)
#
# promise_log.csv is emptied (not deleted), same header-only-rewrite idiom
# api_reset_logs uses for its CSVs, so /api/promise-reply keeps appending to
# it immediately afterward without a "file missing" edge case.
# --------------------------------------------------------------------------
@app.route("/api/reset-conversations", methods=["POST"])
def api_reset_conversations():
    cleared = []

    customer_directory.reset_all()
    cleared.append("customer_directory")

    promise_store.reset_all_promises()
    cleared.append("promises")

    PROMISE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(columns=PROMISE_LOG_COLUMNS).to_csv(PROMISE_LOG_PATH, index=False)
    cleared.append(PROMISE_LOG_PATH.name)

    logger.warning("Customer Conversations reset triggered: cleared %s remote_addr=%s", cleared, request.remote_addr)
    return jsonify({"status": "reset", "cleared": cleared})


# --------------------------------------------------------------------------
# Dashboard "Run batch" panel -- lets an operator kick off
# pipeline/run_batch.py (the Phase 7 orchestrator: scores + SHAP + routes +
# guardrails the whole 280-row synthetic batch, then writes logs/audit_log.csv
# and demo/pitch_numbers.md) from the browser instead of a terminal, and see
# its stdout report. This writes to logs/audit_log.csv, a SEPARATE file from
# logs/webhook_audit_log.csv (the live pipeline's log the rest of the
# dashboard reads) -- see the module docstring at the top of this file -- so
# running a batch here never touches/clears anything the Dashboard/Logs pages
# show.
#
# Runs in a background thread (module already imports run_batch at startup,
# so this reuses that same in-process module rather than shelling out) since
# a full batch run can take a while (some cases route to a real LLM call) and
# must not block the Flask request thread. _batch_run_lock also serializes
# runs -- run_batch.py writes shared CSV/MD files, so two concurrent runs
# could interleave writes.
#
# _batch_run_state is also mirrored to BATCH_STATE_PATH on every change, so
# GET /api/run-batch/status reflects "running" across a browser refresh AND
# a server restart -- an in-memory-only dict would silently reset to "idle"
# on restart even if (from the operator's perspective) nothing ever finished.
# A restart mid-run is the one case that file can't tell the truth about
# (the background thread is gone with the old process) -- _load_batch_state
# detects a persisted "running" status at startup and downgrades it to
# "error" rather than leaving a permanently-stuck "running" indicator.
# --------------------------------------------------------------------------
BATCH_STATE_PATH = LOGS_DIR / "run_batch_state.json"

_batch_run_lock = threading.Lock()
_batch_run_state = {
    "status": "idle",  # idle | running | done | error
    "started_at": None,
    "finished_at": None,
    "duration_seconds": None,
    "output": "",
    "error": None,
}


def _save_batch_state() -> None:
    try:
        BATCH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BATCH_STATE_PATH.write_text(json.dumps(_batch_run_state), encoding="utf-8")
    except Exception:
        logger.exception("Failed to persist run-batch state to %s", BATCH_STATE_PATH)


def _load_batch_state() -> None:
    if not BATCH_STATE_PATH.exists():
        return
    try:
        loaded = json.loads(BATCH_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load persisted run-batch state from %s", BATCH_STATE_PATH)
        return
    if loaded.get("status") == "running":
        loaded["status"] = "error"
        loaded["error"] = "Server restarted while this run was in progress -- outcome unknown."
        loaded["finished_at"] = datetime.now(timezone.utc).isoformat()
    _batch_run_state.update(loaded)


_load_batch_state()


def _run_batch_worker() -> None:
    start = time.monotonic()
    _batch_run_state.update(
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None,
        duration_seconds=None,
        output="",
        error=None,
    )
    _save_batch_state()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            run_batch.main()
        _batch_run_state["status"] = "done"
    except Exception as e:  # noqa: BLE001 -- surfaced to the dashboard, not swallowed
        logger.exception("Batch run failed")
        _batch_run_state["status"] = "error"
        _batch_run_state["error"] = f"{type(e).__name__}: {e}"
    finally:
        _batch_run_state["output"] = buf.getvalue()
        _batch_run_state["finished_at"] = datetime.now(timezone.utc).isoformat()
        _batch_run_state["duration_seconds"] = time.monotonic() - start
        _save_batch_state()
        _batch_run_lock.release()


def _last_batch_run_on_disk() -> dict:
    """Falls back to whatever pipeline/run_batch.py last wrote to disk --
    covers the dashboard being opened fresh (no in-memory run this server
    session yet), e.g. right after a restart, or a run triggered from the
    CLI per the module's own docstring ("Run with: python pipeline/run_batch.py")."""
    info = {"pitch_numbers": None, "generated_at": None, "n_cases": None}
    pn_path = run_batch.PITCH_NUMBERS_PATH
    if pn_path.exists():
        text = pn_path.read_text(encoding="utf-8")
        info["pitch_numbers"] = text
        for line in text.splitlines()[:6]:
            if line.startswith("Generated:"):
                info["generated_at"] = line.split("Generated:", 1)[1].strip()
                break
    if run_batch.AUDIT_LOG_PATH.exists():
        try:
            info["n_cases"] = int(len(pd.read_csv(run_batch.AUDIT_LOG_PATH)))
        except Exception:
            pass
    return info


@app.route("/api/run-batch", methods=["POST"])
def api_run_batch():
    if not _batch_run_lock.acquire(blocking=False):
        return jsonify({"status": "already_running", "message": "A batch run is already in progress."}), 409
    logger.warning("Batch run triggered from dashboard remote_addr=%s", request.remote_addr)
    threading.Thread(target=_run_batch_worker, daemon=True, name="run-batch-worker").start()
    return jsonify({"status": "started"})


@app.route("/api/run-batch/status", methods=["GET"])
def api_run_batch_status():
    state = dict(_batch_run_state)
    state.update(_last_batch_run_on_disk())
    return jsonify(state)


# --------------------------------------------------------------------------
# Dashboard "Model" page -- retrain the official LogReg-vs-XGBoost pair
# (delegates to train_tree_models.main(), completely unchanged) or upload
# and train a custom CSV (models/dashboard_training.py, writing to
# models/custom_runs/<upload_id>/ -- never touches data/train.csv,
# data/holdout.csv, models/artifacts/, or models/model_report.md). Same
# background-thread + lock + persisted-state pattern as Run Batch above.
#
# Retraining the OFFICIAL model overwrites models/artifacts/*.joblib and
# models/model_report.md -- i.e. it replaces what the live pipeline will
# load on its NEXT restart (shap_extract.py caches the pipeline in-process
# once loaded, so an already-running server keeps scoring with whatever it
# already loaded). The frontend confirms before triggering this.
# --------------------------------------------------------------------------
MODEL_TRAIN_STATE_PATH = LOGS_DIR / "model_train_state.json"

_model_train_lock = threading.Lock()
_model_train_state = {
    "status": "idle",  # idle | running | done | error
    "kind": None,  # "official" | "custom"
    "upload_id": None,
    "started_at": None,
    "finished_at": None,
    "duration_seconds": None,
    "output": "",
    "error": None,
}


def _save_model_train_state() -> None:
    try:
        MODEL_TRAIN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        MODEL_TRAIN_STATE_PATH.write_text(json.dumps(_model_train_state), encoding="utf-8")
    except Exception:
        logger.exception("Failed to persist model-train state to %s", MODEL_TRAIN_STATE_PATH)


def _load_model_train_state() -> None:
    if not MODEL_TRAIN_STATE_PATH.exists():
        return
    try:
        loaded = json.loads(MODEL_TRAIN_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load persisted model-train state from %s", MODEL_TRAIN_STATE_PATH)
        return
    if loaded.get("status") == "running":
        loaded["status"] = "error"
        loaded["error"] = "Server restarted while this run was in progress -- outcome unknown."
        loaded["finished_at"] = datetime.now(timezone.utc).isoformat()
    _model_train_state.update(loaded)


_load_model_train_state()


def _model_train_worker(kind: str, upload_id: "str | None" = None) -> None:
    start = time.monotonic()
    _model_train_state.update(
        status="running", kind=kind, upload_id=upload_id,
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None, duration_seconds=None, output="", error=None,
    )
    _save_model_train_state()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            if kind == "official":
                train_tree_models.main()
            else:
                dashboard_training.run_custom_training(upload_id)
        _model_train_state["status"] = "done"
    except Exception as e:  # noqa: BLE001 -- surfaced to the dashboard, not swallowed
        logger.exception("Model training failed (kind=%s upload_id=%s)", kind, upload_id)
        _model_train_state["status"] = "error"
        _model_train_state["error"] = f"{type(e).__name__}: {e}"
    finally:
        _model_train_state["output"] = buf.getvalue()
        _model_train_state["finished_at"] = datetime.now(timezone.utc).isoformat()
        _model_train_state["duration_seconds"] = time.monotonic() - start
        _save_model_train_state()
        _model_train_lock.release()


def _official_report_on_disk() -> dict:
    info = {"report_md": None, "metadata": None}
    report_path = train_tree_models.REPORT_PATH
    metadata_path = train_tree_models.ARTIFACTS_DIR / "model_metadata.json"
    if report_path.exists():
        info["report_md"] = report_path.read_text(encoding="utf-8")
    if metadata_path.exists():
        try:
            info["metadata"] = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return info


@app.route("/api/model/dataset", methods=["GET"])
def api_model_dataset():
    df = train_tree_models.load_data()
    class_counts = df[train_tree_models.TARGET_COL].value_counts().to_dict()
    return jsonify({
        "columns": df.columns.tolist(),
        "rows": json.loads(df.to_json(orient="records")),
        "row_count": len(df),
        "class_counts": {str(int(k)): int(v) for k, v in class_counts.items()},
        "feature_columns": {
            "numeric": train_tree_models.NUMERIC_FEATURES,
            "categorical": train_tree_models.CATEGORICAL_FEATURES,
        },
        "target_column": train_tree_models.TARGET_COL,
        "source": ["data/train.csv", "data/holdout.csv"],
    })


@app.route("/api/model/report", methods=["GET"])
def api_model_report():
    return jsonify(_official_report_on_disk())


@app.route("/api/model/train", methods=["POST"])
def api_model_train():
    if not _model_train_lock.acquire(blocking=False):
        return jsonify({"status": "already_running", "message": "A training run is already in progress."}), 409
    logger.warning("Official model retrain triggered from dashboard remote_addr=%s", request.remote_addr)
    threading.Thread(
        target=_model_train_worker, kwargs={"kind": "official"},
        daemon=True, name="model-train-official",
    ).start()
    return jsonify({"status": "started"})


@app.route("/api/model/train/status", methods=["GET"])
def api_model_train_status():
    state = dict(_model_train_state)
    state["official_report"] = _official_report_on_disk()
    return jsonify(state)


@app.route("/api/model/upload", methods=["POST"])
def api_model_upload():
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400
    if not file.filename.lower().endswith(".csv"):
        return jsonify({"ok": False, "error": "Only .csv files are supported"}), 400
    entry = dashboard_training.save_upload(file)
    if "upload_id" not in entry:
        return jsonify(entry), 400
    logger.info(
        "Dataset uploaded: %s (upload_id=%s valid=%s rows=%s) remote_addr=%s",
        entry.get("filename"), entry.get("upload_id"), entry.get("valid"), entry.get("row_count"),
        request.remote_addr,
    )
    return jsonify(entry)


@app.route("/api/model/uploads", methods=["GET"])
def api_model_uploads():
    return jsonify({"uploads": dashboard_training.list_uploads()})


@app.route("/api/model/train-custom", methods=["POST"])
def api_model_train_custom():
    body = request.get_json(silent=True) or {}
    upload_id = body.get("upload_id")
    if not upload_id:
        return jsonify({"status": "error", "message": "upload_id is required"}), 400
    entry = dashboard_training.get_upload(upload_id)
    if entry is None:
        return jsonify({"status": "error", "message": f"Unknown upload_id={upload_id!r}"}), 404
    if not entry.get("valid"):
        return jsonify({
            "status": "error",
            "message": entry.get("validation", {}).get("error") or "Dataset failed validation",
        }), 400
    if not _model_train_lock.acquire(blocking=False):
        return jsonify({"status": "already_running", "message": "A training run is already in progress."}), 409
    logger.warning("Custom model training triggered: upload_id=%s remote_addr=%s", upload_id, request.remote_addr)
    threading.Thread(
        target=_model_train_worker, kwargs={"kind": "custom", "upload_id": upload_id},
        daemon=True, name=f"model-train-custom-{upload_id}",
    ).start()
    return jsonify({"status": "started", "upload_id": upload_id})


@app.route("/api/model/custom-report/<upload_id>", methods=["GET"])
def api_model_custom_report(upload_id):
    report = dashboard_training.get_custom_report(upload_id)
    if report is None:
        return jsonify({"message": "No completed run for this upload_id yet"}), 404
    return jsonify(report)


# --------------------------------------------------------------------------
# Task 5 -- dashboard page. No authentication (removed at user's request --
# this endpoint, the /api/* endpoints, and the audit/webhook logs are all
# publicly readable/triggerable at https://razor-recover.heracle.fit).
#
# The dashboard is a React + Tailwind SPA (dashboard-app/, built with Vite)
# served here as static files -- `npm run build` inside dashboard-app/
# produces dashboard-app/dist/index.html plus hashed assets under
# dist/assets/, built with base '/dashboard/' so those asset URLs resolve
# to the routes below. /dashboard, /conversations, /logs and /run-batch all
# serve the SAME built index.html -- the SPA's own client-side router
# (AppRouter.jsx) reads window.location.pathname and renders the matching
# page, so all four routes share one design system and one Sidebar (which is
# also how the Sidebar's "Live" indicator can reflect a pause toggle shared
# across pages). The old plain server-rendered dashboard/conversations.html
# and dashboard/logs.html are superseded by dashboard-app/src/pages/.
# --------------------------------------------------------------------------
DASHBOARD_DIST_DIR = BASE_DIR / "dashboard-app" / "dist"


@app.route("/dashboard", methods=["GET"])
@app.route("/dashboard/", methods=["GET"])
@app.route("/conversations", methods=["GET"])
@app.route("/logs", methods=["GET"])
@app.route("/run-batch", methods=["GET"])
@app.route("/model", methods=["GET"])
def dashboard():
    return send_from_directory(DASHBOARD_DIST_DIR, "index.html")


@app.route("/dashboard/assets/<path:filename>", methods=["GET"])
def dashboard_assets(filename):
    return send_from_directory(DASHBOARD_DIST_DIR / "assets", filename)


def _startup_checks() -> None:
    if not RAZORPAY_WEBHOOK_SECRET:
        logger.error("RAZORPAY_WEBHOOK_SECRET is not set -- refusing to start (see .env.example).")
        sys.exit(1)
    logger.info("Warming up pipeline (loading model + fitting SHAP background + confidence-gate band)...")
    band_low, band_high = run_case.get_probability_band()
    logger.info("Confidence-gate probability band fit: low=%.4f high=%.4f", band_low, band_high)
    logger.info("Pipeline warm-up complete.")

    ptp_outcomes.start_background_expiry_checker()


if __name__ == "__main__":
    _startup_checks()
    logger.info("Starting webhook receiver on 0.0.0.0:%d (tunnel: https://razor-recover.heracle.fit)", PORT)
    app.run(host="0.0.0.0", port=PORT)
