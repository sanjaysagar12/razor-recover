"""
Shared case-execution pipeline: confidence_gate -> shap_extract ->
(conditionally) llm_layer -> guardrails -> execute_action -> audit log.

Extracted from webhook_receiver.py so the real webhook path and the
dashboard's synthetic-test-case trigger (POST /api/trigger-test-case) call
the EXACT SAME functions -- neither one duplicates or reimplements pipeline
logic. webhook_receiver.py's map_payload_to_case() builds a case dict from a
raw Razorpay webhook payload; the dashboard trigger builds one directly from
an operator-supplied JSON body. Both hand that case dict to run_recovery_case
below.

Writes to logs/webhook_audit_log.csv -- NOT logs/audit_log.csv, which is
exclusively pipeline/run_batch.py's synthetic-batch output (see that
module's docstring for why mixing live rows into it breaks
validate_audit_log.py). This file's schema is run_batch.AUDIT_COLUMNS (the
same 25 columns, same order) plus columns appended for decline-code-mapper
output, customer-history source, action-execution results, and case
provenance (source: "real_webhook" vs "manual_test").
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

import confidence_gate
import customer_history
import execute_action
import llm_layer
import promise_store
import ptp_trigger
import run_batch
import shap_extract

# Deliberately the same logger name webhook_receiver.py uses for its own
# request-level logs ("webhook_receiver") -- both the real webhook path and
# the dashboard trigger endpoint funnel through this module, so every case's
# step-by-step trace lands in the same logs/webhook_receiver.log stream,
# interleaved in request order, rather than split across two logger names.
logger = logging.getLogger("webhook_receiver")

BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"

# First 25 columns identical, in the same order, to run_batch.AUDIT_COLUMNS
# (logs/audit_log.csv's schema) -- new columns are appended after, never
# inserted/reordered, so nothing reading the first 25 by name breaks.
# "source" was appended for the dashboard trigger endpoint (Task 3/4) --
# existing rows written before it existed were backfilled with
# source="real_webhook" (see the migration run alongside this change; every
# one of those rows did go through the real /webhook/razorpay route, even
# where the payload itself was a hand-crafted test payload).
WEBHOOK_AUDIT_COLUMNS = run_batch.AUDIT_COLUMNS + [
    "decline_code_bucket",
    "decline_code_is_ambiguous",
    "customer_key",
    "customer_history_source",
    "execution_status",
    "execution_mechanism",
    "execution_detail",
    "execution_timestamp",
    "source",
    # PTP offer-eligibility gate (pipeline/ptp_trigger.should_offer_ptp) --
    # only ever set by run_recovery_case below; run_recovered_case (no
    # decline, nothing to gate) and run_promise_reschedule (acting on a
    # promise that was already offered/replied to) leave these None, same
    # as every other column those two rows don't populate.
    "ptp_offer_decision",
    "ptp_trigger_category",
    "ptp_offer_reason",
    # Persisted so a LATER call (webhook_receiver.api_promise_reply, at
    # reply time) can reconstruct the same case dict should_offer_ptp saw
    # at scoring time, instead of guessing at defaults -- retry_attempt_
    # number in particular must be the ORIGINAL value, not a re-guessed 1,
    # or a case that was correctly offered PTP at scoring time (e.g. a 2nd
    # consecutive failure) would wrongly re-evaluate as a first failure
    # when its customer's reply comes in. Same None-for-other-row-types
    # convention as decline_code_bucket etc. above.
    "retry_attempt_number",
    "ltv_tier",
    "payment_rail",
    "cumulative_retries_this_txn",
]

WEBHOOK_AUDIT_LOG_PATH = LOGS_DIR / "webhook_audit_log.csv"

SOURCE_REAL_WEBHOOK = "real_webhook"
SOURCE_MANUAL_TEST = "manual_test"


def _append_webhook_audit_row(row: dict) -> None:
    WEBHOOK_AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Same reindex-onto-the-new-header migration webhook_receiver.py's
    # _append_promise_log already uses for logs/promise_log.csv -- when
    # WEBHOOK_AUDIT_COLUMNS grows (as it just did for the ptp_offer_*
    # columns), a file written under the OLD, shorter header would
    # otherwise end up with data rows wider than its own header line the
    # next time a row is appended, breaking every pd.read_csv() call
    # against it (api_audit_log, api_case_detail, api_customers, ...).
    # Existing rows backfill the new columns as blank, not guessed.
    if WEBHOOK_AUDIT_LOG_PATH.exists():
        existing = pd.read_csv(WEBHOOK_AUDIT_LOG_PATH)
        if list(existing.columns) != WEBHOOK_AUDIT_COLUMNS:
            existing.reindex(columns=WEBHOOK_AUDIT_COLUMNS).to_csv(WEBHOOK_AUDIT_LOG_PATH, index=False)

    df = pd.DataFrame([row], columns=WEBHOOK_AUDIT_COLUMNS)
    write_header = not WEBHOOK_AUDIT_LOG_PATH.exists()
    df.to_csv(WEBHOOK_AUDIT_LOG_PATH, mode="a", header=write_header, index=False)


def blank_webhook_audit_row() -> dict:
    return {col: None for col in WEBHOOK_AUDIT_COLUMNS}


def append_webhook_audit_row(row: dict) -> None:
    """Public entry point for callers that build a row themselves (e.g.
    webhook_receiver.py's pipeline-error path, via build_error_row below) --
    run_recovery_case/run_recovered_case call the private version directly."""
    _append_webhook_audit_row(row)


_band_cache: tuple[float, float] | None = None


def get_probability_band() -> tuple[float, float]:
    """Confidence-gate's probability band, fit once against the historical
    batch (data/train.csv + data/holdout.csv, via shap_extract.get_scores_df)
    and cached for the life of the process. Live/synthetic cases are routed
    against this fixed reference band rather than refitting per-request --
    a single new case has no distribution of its own to compute percentiles
    over."""
    global _band_cache
    if _band_cache is None:
        scores_df = shap_extract.get_scores_df()
        _band_cache = confidence_gate.compute_probability_band(scores_df)
    return _band_cache


# --------------------------------------------------------------------------
# Test-only LLM adapter (dashboard "Malformed LLM output" preset)
# --------------------------------------------------------------------------
class MalformedTestAdapter:
    """Deterministically returns schema-invalid output -- makes NO real LLM
    call. Exists only so the dashboard's "Malformed LLM output" preset can
    reliably exercise llm_layer.validate_llm_output's fallback path
    (llm_schema_valid=False, requires_human_review=True) without depending
    on a real provider happening to misbehave, which is not something that
    can be forced through the normal ClaudeAdapter/GeminiAdapter.

    Duck-types llm_layer.LLMAdapter (a `generate(case) -> dict` method) --
    llm_layer.get_llm_decision() only ever calls that method, so this needs
    no changes to llm_layer.py itself. Selected by run_recovery_case when
    case_facts["_force_malformed_llm"] is True -- a key map_payload_to_case()
    never sets, so a real webhook payload can never trigger it.
    """

    def generate(self, case: dict) -> dict:
        return {
            "case_id": case["case_id"],
            "tree_model_score": case["tree_model_score"],
            "tree_model_top_features": [
                {"feature": f["feature"], "shap_value": f["shap_value"]} for f in case["shap_top_features"]
            ],
            "recommended_action": "not_a_real_action",  # fails schema.LLMDecision's Literal check
            "action_scheduled_for": None,
            "confidence": 1.5,  # also out of [0, 1] -- belt and suspenders
            "reasoning_summary": "TEST: deliberately malformed output (dashboard 'Malformed LLM output' preset).",
            "guardrail_flags": [],
            "requires_human_review": False,
            "model_version": "test_malformed_adapter",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def run_recovery_case(case_facts: dict, event_type: str, source: str = SOURCE_REAL_WEBHOOK) -> dict:
    """Runs one already-mapped case dict through the full pipeline
    (confidence_gate -> shap_extract -> conditionally llm_layer ->
    guardrails -> execute_action) and appends the resulting row to
    logs/webhook_audit_log.csv. Returns that row as a dict.

    case_facts must be shaped like webhook_receiver.map_payload_to_case()'s
    output (see that function's docstring for the full field list) --
    critically including decline_code, decline_code_is_ambiguous,
    customer_key, customer_history_source, and every model feature_column.

    source: "real_webhook" or "manual_test" -- written to the new `source`
    column so triggered test cases are never confused with real customer
    events in the audit trail.
    """
    case_id = case_facts["case_id"]
    customer_key = case_facts.get("customer_key")
    logger.info(
        "case_id=%s: mapped -- source=%s amount=%s decline_code=%s decline_code_bucket=%s payment_rail=%s "
        "retry_attempt_number=%s customer_key=%s customer_history_source=%s",
        case_id, source, case_facts.get("amount"), case_facts.get("decline_code"),
        case_facts.get("decline_code_bucket"), case_facts.get("payment_rail"),
        case_facts.get("retry_attempt_number"), customer_key, case_facts.get("customer_history_source"),
    )

    logger.info("case_id=%s: step 1/6 -- scoring via shap_extract.score_new_case", case_id)
    scoring = shap_extract.score_new_case(case_facts)
    tree_model_score = scoring["tree_model_score"]
    shap_top_features = scoring["shap_top_features"]
    logger.info(
        "case_id=%s: scored -- tree_model_score=%.4f top_shap_feature=%s",
        case_id, tree_model_score, shap_top_features[0] if shap_top_features else None,
    )

    logger.info("case_id=%s: step 2/6 -- routing via confidence_gate.route_case", case_id)
    band_low, band_high = get_probability_band()
    record = confidence_gate.route_case(
        case_id, tree_model_score, case_facts["decline_code"], band_low, band_high,
        is_ambiguous=case_facts.get("decline_code_is_ambiguous"),
        risk_tier=case_facts.get("current_risk_tier"),
    )
    logger.info(
        "case_id=%s: routed -- routed_to_llm=%s routing_trigger=%s template_action=%s",
        case_id, record["routed_to_llm"], record["routing_trigger"], record["template_action"],
    )

    meta = shap_extract.load_metadata()
    tree_model_version = meta["models"][meta["primary_model_key"]]["version"]
    if record["routed_to_llm"]:
        if case_facts.get("_force_malformed_llm"):
            logger.info("case_id=%s: step 3/6 -- routed to LLM layer, using MalformedTestAdapter (forced)", case_id)
            adapter = MalformedTestAdapter()
        else:
            logger.info(
                "case_id=%s: step 3/6 -- routed to LLM layer, provider=%s",
                case_id, os.environ.get("LLM_PROVIDER", "claude"),
            )
            adapter = llm_layer.get_llm_adapter()
    else:
        logger.info(
            "case_id=%s: step 3/6 -- not routed to LLM, using template_action=%s", case_id, record["template_action"]
        )
        adapter = None

    logger.info("case_id=%s: step 4/6 -- applying guardrails and building audit row", case_id)
    audit_row = run_batch.build_audit_row(
        record, shap_top_features, case_facts, tree_model_version, adapter, band_low, band_high
    )
    logger.info(
        "case_id=%s: guardrails applied -- proposed_action=%s final_action=%s guardrail_overrode=%s "
        "guardrail_flags=%s requires_human_review=%s llm_schema_valid=%s",
        case_id, audit_row["proposed_action"], audit_row["final_action"], audit_row["guardrail_overrode"],
        audit_row["guardrail_flags"], audit_row["requires_human_review"], audit_row["llm_schema_valid"],
    )

    # PTP offer-eligibility gate -- immediately after the guardrail layer,
    # before any chat UI would render. Purely a decision-transparency
    # record for this phase (see pipeline/ptp_trigger.py's module docstring
    # for what's explicitly out of scope): it does not affect final_action,
    # execution, or anything below.
    ptp_offer = ptp_trigger.should_offer_ptp(case_facts)
    logger.info(
        "case_id=%s: PTP-offer decision -- offer_ptp=%s trigger_category=%s reason=%s",
        case_id, ptp_offer["offer_ptp"], ptp_offer["trigger_category"], ptp_offer["reason"],
    )

    logger.info(
        "case_id=%s: step 5/6 -- executing final_action=%s via execute_action", case_id, audit_row["final_action"]
    )
    execution_result = execute_action.execute_action(
        case_facts, audit_row["final_action"], audit_row.get("action_scheduled_for")
    )
    logger.info(
        "case_id=%s: execution complete -- status=%s mechanism=%s detail=%s",
        case_id, execution_result["execution_status"], execution_result["execution_mechanism"],
        execution_result["execution_detail"],
    )

    logger.info("case_id=%s: step 6/6 -- recording customer_history outcome (recovered=False)", case_id)
    # This case's own outcome (a failure -- it's here because the payment
    # failed) updates the customer's running honor rate for the NEXT case,
    # after today's scoring already used the pre-event fields. Skipped for
    # manual_test cases with no real customer_key (most presets use a
    # synthetic key on purpose so they don't pollute a real customer's
    # history -- see webhook_receiver.py's preset definitions).
    if customer_key:
        customer_history.record_payment_outcome(customer_key, recovered=False)

    webhook_row = {col: audit_row.get(col) for col in run_batch.AUDIT_COLUMNS}
    webhook_row.update(
        {
            "decline_code_bucket": case_facts.get("decline_code_bucket"),
            "decline_code_is_ambiguous": case_facts.get("decline_code_is_ambiguous"),
            "customer_key": customer_key,
            "customer_history_source": case_facts.get("customer_history_source"),
            "execution_status": execution_result["execution_status"],
            "execution_mechanism": execution_result["execution_mechanism"],
            "execution_detail": execution_result["execution_detail"],
            "execution_timestamp": execution_result["execution_timestamp"],
            "source": source,
            "ptp_offer_decision": ptp_offer["offer_ptp"],
            "ptp_trigger_category": ptp_offer["trigger_category"],
            "ptp_offer_reason": ptp_offer["reason"],
            "retry_attempt_number": case_facts.get("retry_attempt_number"),
            "ltv_tier": case_facts.get("ltv_tier"),
            "payment_rail": case_facts.get("payment_rail"),
            "cumulative_retries_this_txn": case_facts.get("cumulative_retries_this_txn"),
        }
    )
    _append_webhook_audit_row(webhook_row)
    logger.info(
        "case_id=%s event=%s DONE -- routed_to_llm=%s final_action=%s requires_human_review=%s "
        "guardrail_flags=%s execution_status=%s",
        case_id, event_type, record["routed_to_llm"], audit_row["final_action"],
        audit_row["requires_human_review"], audit_row["guardrail_flags"], execution_result["execution_status"],
    )
    return webhook_row


def run_recovered_case(
    case_id: str,
    amount: float,
    customer_key: str | None,
    event_type: str,
    source: str = SOURCE_REAL_WEBHOOK,
) -> dict:
    """subscription.charged-shaped case -- payment recovered, no pipeline
    routing or execution needed. Logged as a recovered-amount row and
    updates the customer's history record."""
    logger.info("case_id=%s: source=%s amount=%s customer_key=%s", case_id, source, amount, customer_key)
    _unused_fields, is_first_seen = customer_history.get_or_create_customer(customer_key)
    if customer_key:
        customer_history.record_payment_outcome(customer_key, recovered=True)

    row = blank_webhook_audit_row()
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
            "routing_rationale": f"{event_type} webhook -- payment recovered, no pipeline routing needed",
            "customer_key": customer_key,
            "customer_history_source": "first_seen_defaults" if is_first_seen else "existing_history",
            "execution_status": "not_applicable",
            "execution_mechanism": "none",
            "execution_detail": "final_action=recovered -- payment already succeeded, no action to execute.",
            "execution_timestamp": datetime.now(timezone.utc).isoformat(),
            "source": source,
        }
    )
    _append_webhook_audit_row(row)
    logger.info("case_id=%s event=%s DONE -- RECOVERED amount=%s customer_key=%s", case_id, event_type, amount, customer_key)
    return row


def run_promise_reschedule(
    case: dict, promise: dict, guardrail_result: dict, source: str = SOURCE_REAL_WEBHOOK
) -> dict:
    """Executes + audits a guardrail-approved PTP reschedule -- same
    guardrails -> execute_action -> audit-log shape as run_recovery_case's
    steps 4/5/6 above, just entered from the promise-reply flow
    (webhook_receiver.py's /api/promise-reply) instead of a payment-failure
    webhook event.

    Only ever called when guardrail_result["guardrail_status"] == "approved"
    -- that check is the caller's routing decision (webhook_receiver.py);
    execute_action.execute_promise_reschedule itself still refuses to run
    for anything else as a second, defensive check.

    case: case-shaped dict for promise["case_id"] (amount required;
    customer_name/email/contact used opportunistically -- see
    webhook_receiver._promise_date_case_context for how the live path builds
    this). promise: a promise_store row (promise_id, case_id, customer_id,
    extracted_date). guardrail_result: guardrails.apply_ptp_guardrails'
    output for this promise.

    Writes one row to logs/webhook_audit_log.csv -- the same live audit
    trail run_recovery_case writes to, NOT logs/audit_log.csv (exclusively
    run_batch.py's frozen synthetic-batch output -- see this module's
    docstring). proposed_action carries the guardrail-approved decision
    ("reschedule_to_<date>"); execution_status/execution_mechanism carry
    what actually happened at the API level -- kept as two distinct fields,
    the same audit-honesty split execution_mechanism already gives
    retry_now (see execute_action.py's module docstring).

    Also updates the promises table: payment_link_id on success (outcome
    stays 'pending'), or outcome='reschedule_failed' on failure -- never a
    false-positive "scheduled" state for a failed API call.
    """
    case_id = promise["case_id"]
    promise_id = promise["promise_id"]
    extracted_date = promise.get("extracted_date")
    logger.info(
        "case_id=%s promise_id=%s: executing PTP reschedule -- extracted_date=%s guardrail_status=%s",
        case_id, promise_id, extracted_date, guardrail_result.get("guardrail_status"),
    )

    execution_result = execute_action.execute_promise_reschedule(case, promise, guardrail_result)
    logger.info(
        "case_id=%s promise_id=%s: PTP reschedule execution complete -- status=%s mechanism=%s",
        case_id, promise_id, execution_result["execution_status"], execution_result["execution_mechanism"],
    )

    if execution_result["execution_status"] == "success":
        promise_store.update_promise_payment_link(promise_id, execution_result["payment_link_id"])
        promise_store.update_promise_status(promise_id, promise_store.STATUS_SCHEDULED)
    else:
        promise_store.mark_promise_reschedule_failed(promise_id)

    row = blank_webhook_audit_row()
    row.update(
        {
            "case_id": case_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "amount": case.get("amount"),
            "decline_code": case.get("decline_code"),
            "routed_to_llm": False,
            "guardrail_flags": guardrail_result.get("guardrail_flags", []),
            "proposed_action": f"reschedule_to_{extracted_date}",
            "final_action": guardrail_result.get("final_action"),
            "guardrail_overrode": False,
            "requires_human_review": guardrail_result.get("requires_human_review", False),
            "pipeline_version": run_batch.PIPELINE_VERSION,
            "routing_rationale": f"PTP guardrail-approved reschedule for promise_id={promise_id}",
            "customer_key": promise.get("customer_id"),
            "execution_status": execution_result["execution_status"],
            "execution_mechanism": execution_result["execution_mechanism"],
            "execution_detail": execution_result["execution_detail"],
            "execution_timestamp": execution_result["execution_timestamp"],
            "source": source,
        }
    )
    _append_webhook_audit_row(row)
    logger.info(
        "case_id=%s promise_id=%s DONE -- proposed_action=%s final_action=%s execution_status=%s",
        case_id, promise_id, row["proposed_action"], row["final_action"], row["execution_status"],
    )
    return row


def build_error_row(case_id: str, event_type: str, source: str = SOURCE_REAL_WEBHOOK) -> dict:
    """A pipeline bug must never crash the caller -- this is the row shape
    written when run_recovery_case raises before reaching a final_action."""
    row = blank_webhook_audit_row()
    row.update(
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
            "source": source,
        }
    )
    return row
