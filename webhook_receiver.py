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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from flask import Flask, jsonify, request

BASE_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = BASE_DIR / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

import confidence_gate  # noqa: E402
import customer_history  # noqa: E402
import decline_code_mapper  # noqa: E402
import execute_action  # noqa: E402
import llm_layer  # noqa: E402
import run_batch  # noqa: E402
import shap_extract  # noqa: E402

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
# Event routing
# --------------------------------------------------------------------------
RECOVERY_EVENTS = {"payment.failed", "subscription.pending", "subscription.halted"}
RECOVERED_EVENT = "subscription.charged"

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
# Pipeline orchestration
# --------------------------------------------------------------------------
_band_cache: tuple[float, float] | None = None


def _get_probability_band() -> tuple[float, float]:
    """Confidence-gate's probability band, fit once against the historical
    batch (data/train.csv + data/holdout.csv, via shap_extract.get_scores_df)
    and cached for the life of the process. Live webhook cases are routed
    against this fixed reference band rather than refitting per-request --
    a single new case has no distribution of its own to compute percentiles
    over."""
    global _band_cache
    if _band_cache is None:
        scores_df = shap_extract.get_scores_df()
        _band_cache = confidence_gate.compute_probability_band(scores_df)
        logger.info("Confidence-gate probability band fit: low=%.4f high=%.4f", *_band_cache)
    return _band_cache


# First 25 columns identical, in the same order, to run_batch.AUDIT_COLUMNS
# (logs/audit_log.csv's schema) -- new columns are appended after, never
# inserted/reordered, so nothing reading the first 25 by name breaks.
WEBHOOK_AUDIT_COLUMNS = run_batch.AUDIT_COLUMNS + [
    "decline_code_bucket",
    "decline_code_is_ambiguous",
    "customer_key",
    "customer_history_source",
    "execution_status",
    "execution_mechanism",
    "execution_detail",
    "execution_timestamp",
]

WEBHOOK_AUDIT_LOG_PATH = LOGS_DIR / "webhook_audit_log.csv"


def _append_webhook_audit_row(row: dict) -> None:
    WEBHOOK_AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([row], columns=WEBHOOK_AUDIT_COLUMNS)
    write_header = not WEBHOOK_AUDIT_LOG_PATH.exists()
    df.to_csv(WEBHOOK_AUDIT_LOG_PATH, mode="a", header=write_header, index=False)


def _blank_webhook_audit_row() -> dict:
    return {col: None for col in WEBHOOK_AUDIT_COLUMNS}


def process_recovery_case(event: dict, event_type: str) -> dict:
    """payment.failed / subscription.pending / subscription.halted -- runs
    the full pipeline (confidence_gate -> shap_extract -> conditionally
    llm_layer -> guardrails -> execute_action) and appends the resulting row
    to logs/webhook_audit_log.csv.

    execute_action's result is a fact separate from the guardrail decision
    (execution_status/execution_mechanism/execution_detail/
    execution_timestamp columns) -- a decision and its execution can
    diverge, e.g. final_action=retry_now but the payment-link API call
    fails, so they're never merged into one field.
    """
    logger.info("event=%s: step 1/7 -- mapping payload to case schema", event_type)
    case_facts = map_payload_to_case(event)
    case_id = case_facts["case_id"]
    customer_key = case_facts["customer_key"]
    logger.info(
        "case_id=%s: mapped -- amount=%.2f decline_code=%s decline_code_bucket=%s payment_rail=%s "
        "retry_attempt_number=%s customer_key=%s customer_history_source=%s",
        case_id, case_facts["amount"], case_facts["decline_code"], case_facts["decline_code_bucket"],
        case_facts["payment_rail"], case_facts["retry_attempt_number"], customer_key,
        case_facts["customer_history_source"],
    )

    logger.info("case_id=%s: step 2/7 -- scoring via shap_extract.score_new_case", case_id)
    scoring = shap_extract.score_new_case(case_facts)
    tree_model_score = scoring["tree_model_score"]
    shap_top_features = scoring["shap_top_features"]
    logger.info(
        "case_id=%s: scored -- tree_model_score=%.4f top_shap_feature=%s",
        case_id, tree_model_score, shap_top_features[0] if shap_top_features else None,
    )

    logger.info("case_id=%s: step 3/7 -- routing via confidence_gate.route_case", case_id)
    band_low, band_high = _get_probability_band()
    record = confidence_gate.route_case(
        case_id, tree_model_score, case_facts["decline_code"], band_low, band_high,
        is_ambiguous=case_facts["decline_code_is_ambiguous"],
    )
    logger.info(
        "case_id=%s: routed -- routed_to_llm=%s routing_trigger=%s template_action=%s",
        case_id, record["routed_to_llm"], record["routing_trigger"], record["template_action"],
    )

    meta = shap_extract.load_metadata()
    tree_model_version = meta["models"][meta["primary_model_key"]]["version"]
    if record["routed_to_llm"]:
        logger.info(
            "case_id=%s: step 4/7 -- routed to LLM layer, provider=%s",
            case_id, os.environ.get("LLM_PROVIDER", "claude"),
        )
        adapter = llm_layer.get_llm_adapter()
    else:
        logger.info(
            "case_id=%s: step 4/7 -- not routed to LLM, using template_action=%s",
            case_id, record["template_action"],
        )
        adapter = None

    logger.info("case_id=%s: step 5/7 -- applying guardrails and building audit row", case_id)
    audit_row = run_batch.build_audit_row(
        record, shap_top_features, case_facts, tree_model_version, adapter, band_low, band_high
    )
    logger.info(
        "case_id=%s: guardrails applied -- proposed_action=%s final_action=%s guardrail_overrode=%s "
        "guardrail_flags=%s requires_human_review=%s llm_schema_valid=%s",
        case_id, audit_row["proposed_action"], audit_row["final_action"], audit_row["guardrail_overrode"],
        audit_row["guardrail_flags"], audit_row["requires_human_review"], audit_row["llm_schema_valid"],
    )

    logger.info("case_id=%s: step 6/7 -- executing final_action=%s via execute_action", case_id, audit_row["final_action"])
    execution_result = execute_action.execute_action(
        case_facts, audit_row["final_action"], audit_row.get("action_scheduled_for")
    )
    logger.info(
        "case_id=%s: execution complete -- status=%s mechanism=%s detail=%s",
        case_id, execution_result["execution_status"], execution_result["execution_mechanism"],
        execution_result["execution_detail"],
    )

    logger.info("case_id=%s: step 7/7 -- recording customer_history outcome (recovered=False)", case_id)
    # This case's own outcome (a failure -- it's here because the payment
    # failed) updates the customer's running honor rate for the NEXT case,
    # after today's scoring already used the pre-event fields.
    customer_history.record_payment_outcome(customer_key, recovered=False)

    webhook_row = {col: audit_row.get(col) for col in run_batch.AUDIT_COLUMNS}
    webhook_row.update(
        {
            "decline_code_bucket": case_facts["decline_code_bucket"],
            "decline_code_is_ambiguous": case_facts["decline_code_is_ambiguous"],
            "customer_key": customer_key,
            "customer_history_source": case_facts["customer_history_source"],
            "execution_status": execution_result["execution_status"],
            "execution_mechanism": execution_result["execution_mechanism"],
            "execution_detail": execution_result["execution_detail"],
            "execution_timestamp": execution_result["execution_timestamp"],
        }
    )
    _append_webhook_audit_row(webhook_row)
    logger.info("case_id=%s: audit row appended to %s", case_id, WEBHOOK_AUDIT_LOG_PATH)

    logger.info(
        "case_id=%s event=%s DONE -- routed_to_llm=%s final_action=%s requires_human_review=%s "
        "guardrail_flags=%s execution_status=%s execution_mechanism=%s",
        case_id, event_type, record["routed_to_llm"], audit_row["final_action"],
        audit_row["requires_human_review"], audit_row["guardrail_flags"],
        execution_result["execution_status"], execution_result["execution_mechanism"],
    )
    return webhook_row


def process_recovered_case(event: dict, event_type: str) -> dict:
    """subscription.charged -- payment recovered, no pipeline routing or
    execution needed (the payment already succeeded). Logged as a
    recovered-amount row in logs/webhook_audit_log.csv for recovered-amount
    tracking, and updates the customer's history record."""
    logger.info("event=%s: step 1/3 -- extracting recovered-payment facts", event_type)
    payment_entity, subscription_entity = _extract_entities(event)
    case_id = _extract_case_id(event)
    amount_paise = payment_entity.get("amount") or subscription_entity.get("amount") or 0
    amount = round(amount_paise / 100.0, 2)
    customer_id = payment_entity.get("customer_id") or subscription_entity.get("customer_id")
    customer_key = _customer_key(customer_id, subscription_entity.get("id"))
    logger.info("case_id=%s: amount=%.2f customer_key=%s", case_id, amount, customer_key)

    logger.info("case_id=%s: step 2/3 -- recording customer_history outcome (recovered=True)", case_id)
    _unused_fields, is_first_seen = customer_history.get_or_create_customer(customer_key)
    customer_history.record_payment_outcome(customer_key, recovered=True)

    logger.info("case_id=%s: step 3/3 -- building and appending audit row (no pipeline routing/execution)", case_id)
    row = _blank_webhook_audit_row()
    row.update(
        {
            "case_id": case_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "amount": amount,
            "routed_to_llm": False,
            "guardrail_flags": "",
            "proposed_action": "recovered",
            "final_action": "recovered",
            "guardrail_overrode": False,
            "requires_human_review": False,
            "pipeline_version": run_batch.PIPELINE_VERSION,
            "routing_rationale": "subscription.charged webhook -- payment recovered, no pipeline routing needed",
            "customer_key": customer_key,
            "customer_history_source": "first_seen_defaults" if is_first_seen else "existing_history",
            "execution_status": "not_applicable",
            "execution_mechanism": "none",
            "execution_detail": "final_action=recovered -- payment already succeeded, no action to execute.",
            "execution_timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    _append_webhook_audit_row(row)
    logger.info("case_id=%s: audit row appended to %s", case_id, WEBHOOK_AUDIT_LOG_PATH)

    logger.info(
        "case_id=%s event=%s DONE -- RECOVERED amount=%.2f customer_key=%s",
        case_id, event_type, amount, customer_key,
    )
    return row


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
        return jsonify({"status": "error", "message": "invalid signature"}), 400

    logger.info("Signature verification OK remote_addr=%s", request.remote_addr)

    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("Body is not valid JSON after signature verification: %s", exc)
        return jsonify({"status": "error", "message": "invalid JSON"}), 400

    event_type = event.get("event", "unknown")
    logger.info(
        "Event parsed: event=%s account_id=%s contains=%s created_at=%s",
        event_type, event.get("account_id"), event.get("contains"), event.get("created_at"),
    )

    if event_type in RECOVERY_EVENTS:
        case_id = _extract_case_id(event)
        try:
            audit_row = process_recovery_case(event, event_type)
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
        except Exception:
            # A pipeline bug must never cause Razorpay to retry-storm us --
            # always ack 200, but flag the case for a human to look at.
            logger.exception("Pipeline error processing case_id=%s event=%s", case_id, event_type)
            error_row = _blank_webhook_audit_row()
            error_row.update(
                {
                    "case_id": case_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "routing_rationale": f"PIPELINE ERROR ({event_type})",
                    "guardrail_flags": "",
                    "proposed_action": "error",
                    "final_action": "error_requires_human_review",
                    "guardrail_overrode": False,
                    "requires_human_review": True,
                    "pipeline_version": run_batch.PIPELINE_VERSION,
                    "execution_status": "not_applicable",
                    "execution_mechanism": "none",
                    "execution_detail": "Pipeline raised before a final_action was reached -- nothing to execute.",
                    "execution_timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            _append_webhook_audit_row(error_row)
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
        return jsonify({"status": "recovered", "case_id": row["case_id"], "event": event_type, "amount": row["amount"]}), 200

    else:
        logger.info("Event=%s -- no handler, acking as ignored", event_type)
        return jsonify({"status": "ignored", "event": event_type}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


def _startup_checks() -> None:
    if not RAZORPAY_WEBHOOK_SECRET:
        logger.error("RAZORPAY_WEBHOOK_SECRET is not set -- refusing to start (see .env.example).")
        sys.exit(1)
    logger.info("Warming up pipeline (loading model + fitting SHAP background + confidence-gate band)...")
    _get_probability_band()
    logger.info("Pipeline warm-up complete.")


if __name__ == "__main__":
    _startup_checks()
    logger.info("Starting webhook receiver on 0.0.0.0:%d (tunnel: https://razor-recover.heracle.fit)", PORT)
    app.run(host="0.0.0.0", port=PORT)
