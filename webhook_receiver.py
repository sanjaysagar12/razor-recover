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

import hashlib
import hmac
import json
import logging
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request

BASE_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = BASE_DIR / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

import confidence_gate  # noqa: E402
import customer_history  # noqa: E402
import decline_code_mapper  # noqa: E402
import execute_action  # noqa: E402
import guardrails  # noqa: E402
import llm_layer  # noqa: E402
import promise_store  # noqa: E402
import ptp_outcomes  # noqa: E402
import run_case  # noqa: E402

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
    if is_first_seen:
        logger.info(
            "case_id=%s: first-seen customer_key=%r -- created new customer_history record with neutral "
            "defaults (customer_tenure_days=0, ltv_tier=medium, historical_ptp_honor_rate=0.5, "
            "prior_retry_success_count=0). This is DIFFERENT from 'we have real history and it happens to "
            "look like this' -- see customer_history_source in the audit row.",
            case_id, customer_key,
        )

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
        "email": payment_entity.get("email"),
        "contact": payment_entity.get("contact"),
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

    if audit_row is None and not webhook_log_rows and not log_lines:
        return jsonify({"error": "not_found", "message": f"No data found for case_id={case_id!r}"}), 404

    return jsonify(
        {"case_id": case_id, "audit_row": audit_row, "webhook_log_rows": webhook_log_rows, "log_lines": log_lines}
    )


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
# Task 5 -- dashboard page. No authentication (removed at user's request --
# this endpoint, the /api/* endpoints, and the audit/webhook logs are all
# publicly readable/triggerable at https://razor-recover.heracle.fit).
# --------------------------------------------------------------------------
DASHBOARD_HTML_PATH = BASE_DIR / "dashboard" / "index.html"


@app.route("/dashboard", methods=["GET"])
def dashboard():
    return Response(DASHBOARD_HTML_PATH.read_text(encoding="utf-8"), mimetype="text/html")


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
