"""
Maps a real Razorpay `error_reason` (from a payment.failed webhook, or the
`error_code` fallback when `error_reason` is absent) to this pipeline's
TRAINING-TIME decline_code_bucket taxonomy -- CLEAR_SOFT / CLEAR_HARD /
AMBIGUOUS, the exact three values used by data/generate_synthetic.py's
BUCKETS and present in the trained model's decline_code_bucket categorical
feature (models/artifacts/model_metadata.json). Do not invent new bucket
names here -- the model has never seen anything else.

Why this exists: pipeline/shap_extract.py's decline_code_bucket feature and
pipeline/confidence_gate.py's ambiguous-code routing trigger were both
designed around a small SYNTHETIC decline-code vocabulary
(insufficient_funds, 05_do_not_honor, stolen_card, ...). Real Razorpay
webhooks report `error_reason` values from a much larger, differently-named
vocabulary that never matches that synthetic set -- so every real case fell
through to the same default bucket and the ambiguous-code routing trigger
structurally never fired. This module is the fix: bucket real error_reason
values directly, and expose an explicit is_ambiguous flag for
confidence_gate.route_case to route on (see webhook_receiver.py /
confidence_gate.py's is_ambiguous parameter).

Source for the error_reason enumeration: Razorpay's published error list at
https://razorpay.com/docs/errors/payments/list/ (fetched 2026-08-28). This
is a best-effort bucketing, not something Razorpay itself labels -- treat
_HARD_REASONS / _SOFT_REASONS / _AMBIGUOUS_REASONS below as a starting point
to correct against real traffic, not ground truth.
"""

from __future__ import annotations

# Exact bucket names from data/generate_synthetic.py's BUCKETS /
# model_metadata.json's decline_code_bucket categorical values.
CLEAR_SOFT = "CLEAR_SOFT"
CLEAR_HARD = "CLEAR_HARD"
AMBIGUOUS = "AMBIGUOUS"

# --------------------------------------------------------------------------
# CLEAR_HARD -- the payment instrument or account is fundamentally unusable.
# Mirrors the training-time semantics of stolen_card/lost_card/
# restricted_card/invalid_account: retrying is not expected to help, and
# blind-retrying risks issuer-side fraud flags.
# --------------------------------------------------------------------------
_HARD_REASONS = {
    "card_number_invalid",
    "card_expired",
    "incorrect_card_expiry_date",
    "card_not_enrolled",
    "card_type_invalid",
    "card_network_not_enabled",
    "debit_instrument_blocked",
    "debit_instrument_inactive",
    "beneficiary_account_does_not_exist",
    "beneficiary_account_dormant",
    "bank_account_invalid",
    "bank_account_validation_failed",
    "record_not_found",
    "user_not_registered_for_netbanking",
    "invalid_vpa",
    "vpa_resolution_failed",
    "international_transaction_not_allowed",
    "recurring_payment_not_enabled",
    "transaction_on_vpa_restricted",
    "payment_method_not_enabled",
    "user_not_eligible",
}

# --------------------------------------------------------------------------
# CLEAR_SOFT -- transient, customer- or network-side, often resolves itself
# on a later attempt with no change in behavior needed. Mirrors
# insufficient_funds/51_insufficient_funds/expired_card_soft.
# --------------------------------------------------------------------------
_SOFT_REASONS = {
    "insufficient_funds",
    "transaction_daily_limit_exceeded",
    "transaction_limit_exceeded",
    "transaction_daily_count_exceeded",
    "transaction_frequency_limit_exceeded",
    "credit_limit_exceeded",
    "credit_limit_expired",
    "credit_limit_inactive",
    "credit_limit_not_approved",
    "credit_not_permitted",
    "otp_expired",
    "incorrect_otp",
    "otp_attempts_exceeded",
    "incorrect_pin",
    "pin_attempts_exceeded",
    "pin_not_set",
    "payment_timed_out",
    "payment_session_expired",
    "payment_collect_request_expired",
    "collect_request_pending",
    "psp_app_not_available",
    "psp_not_available",
    "bank_not_available",
    "bank_cutoff_in_progress",
    "request_timed_out",
    "upi_app_technical_error",
    "funds_blocked_by_mandate",
}

# --------------------------------------------------------------------------
# AMBIGUOUS -- issuer-discretion-type declines, generic/unclear reasons, or
# reasons that plausibly could be either soft or hard depending on context
# the webhook doesn't expose. Mirrors the training-time
# 05_do_not_honor/generic_decline/issuer_unavailable bucket: never trusted
# on score alone, always routed to the LLM. Also the fallback default for
# anything not explicitly bucketed above -- an unrecognized reason should
# widen routing eligibility, never silently default to a confident bucket.
# --------------------------------------------------------------------------
_AMBIGUOUS_REASONS = {
    "payment_failed",
    "card_declined",
    "issuer_declined",
    "payment_declined",
    "payment_declined_due_to_high_traffic",
    "gateway_technical_error",
    "issuer_technical_error",
    "bank_technical_error",
    "server_error",
    "authentication_failed",
    "authorisation_declined_by_psp",
    "payment_risk_check_failed",
    "verification_failed",
    "invalid_response_from_gateway",
    "payment_pending_approval",
    "payment_pending",
    "mandate_creation_declined",
    "mandate_creation_expired",
    "mandate_creation_failed",
    "mandate_creation_timeout",
    "reqauth_mandate_not_acknowledged",
    "deemed_transaction",
    "duplicate_rrn_found",
    "capture_failed",
    "credit_failed",
    "debit_declined",
    "psp_app_not_supported",
    "psp_not_registered",
}

_BUCKET_LOOKUP: dict[str, str] = {
    **{reason: CLEAR_HARD for reason in _HARD_REASONS},
    **{reason: CLEAR_SOFT for reason in _SOFT_REASONS},
    **{reason: AMBIGUOUS for reason in _AMBIGUOUS_REASONS},
}


def map_razorpay_error_reason(error_reason: str, error_source: str | None = None) -> dict:
    """Maps a Razorpay error_reason (case-insensitive) to this pipeline's
    training-time decline_code_bucket taxonomy.

    error_source is accepted but currently unused -- reserved for a future
    refinement (e.g. the same error_reason string could plausibly bucket
    differently depending on whether it originated at "gateway" vs
    "business"/"customer"); kept in the signature now so callers don't need
    to change when that lands.

    Returns {"decline_code_bucket": one of CLEAR_SOFT/CLEAR_HARD/AMBIGUOUS,
    "is_ambiguous": bool, "matched": bool} -- matched=False means the input
    fell through to the AMBIGUOUS default rather than hitting an explicit
    entry above (worth logging at the call site so unrecognized reasons are
    visible and this table can be extended against real traffic).
    """
    reason_key = (error_reason or "").strip().lower()
    bucket = _BUCKET_LOOKUP.get(reason_key)
    matched = bucket is not None
    if bucket is None:
        bucket = AMBIGUOUS

    return {
        "decline_code_bucket": bucket,
        "is_ambiguous": bucket == AMBIGUOUS,
        "matched": matched,
    }
