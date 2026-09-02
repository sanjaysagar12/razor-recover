"""
Phase 6 -- Guardrail Layer.

Deterministic, rule-based safety net applied AFTER Phase 4 (confidence gate)
/ Phase 5 (LLM layer) produce a proposed action. Neither the tree model's
template action nor the LLM's recommendation ever acts directly -- every
case passes through apply_guardrails() first, which can force a different
final_action (a hard override) or flag the case for human review without
touching the action (an escalation-only rule).

Rules are plain (name, condition, override) tuples, not nested if/else, so
each one is independently testable and the whole rule set can be printed or
inspected as one object (see GUARDRAIL_RULES below). Every rule is always
evaluated -- a case can accumulate several flags even though only the
FIRST rule (in list order) whose override touches final_action actually
wins that override. requires_human_review_floor is the exception: it only
ever sets requires_human_review, never final_action.

Phase 13 extends this module with a second, parallel rule pass --
PTP_GUARDRAIL_RULES / apply_ptp_guardrails() -- for promise-to-pay date
extractions (llm_layer.extract_promise_date's output) rather than
recommended-action decisions. See the "Phase 13" section below for why it's
a separate rule list/apply function rather than reusing GUARDRAIL_RULES.

Smoke test:
    python tests/test_guardrails.py
    python pipeline/test_ptp_guardrails.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schema import RECOMMENDED_ACTIONS

# --------------------------------------------------------------------------
# Configurable thresholds
# --------------------------------------------------------------------------

# Decline codes that are never worth retrying automatically -- the
# CLEAR_HARD bucket of the existing decline-code taxonomy (see
# data/generate_synthetic.py). Deliberately excludes soft/ambiguous codes
# like insufficient_funds, expired_card_soft or 05_do_not_honor -- the
# latter is the known-ambiguous code that should route to the LLM, not get
# hard-excluded here.
HARD_DECLINE_CODES: set[str] = {"lost_card", "stolen_card", "restricted_card", "invalid_account"}

# NPCI allows 1 original + 3 retries for UPI Autopay; the 5th attempt trips
# the network's own cap.
NPCI_RETRY_CAP = 4

# NPCI peak execution window, IST (UTC+5:30) -- where UPI Autopay retries
# queue up and are prone to congestion-driven failure. action_scheduled_for
# is converted to IST before this is applied (see _scheduled_hour_ist).
NPCI_PEAK_WINDOW_START_HOUR = 10
NPCI_PEAK_WINDOW_END_HOUR = 13  # exclusive -- window is [10:00, 13:00) IST
IST = timezone(timedelta(hours=5, minutes=30))

# Network-wide retry budget. The data model has no customer/timestamp
# linkage across transactions (case_id is one transaction, not a customer),
# so a true trailing-30-day rolling count isn't derivable -- this reads
# cumulative_retries_this_txn (same-transaction retry count; see
# shap_extract.get_case_facts) as an honest same-transaction proxy instead.
NETWORK_RETRY_CAP_THIS_TXN = 5

# Below this confidence, an LLM/tree proposal is not trusted enough to act
# on without a human in the loop.
CONFIDENCE_FLOOR = 0.4

NO_RETRY_ACTION = "no_retry_prompt_update"

RuleCondition = Callable[[dict, dict], bool]
RuleOverride = Callable[[dict, dict], dict]
GuardrailRule = tuple[str, RuleCondition, RuleOverride]


def _scheduled_hour_ist(action_scheduled_for: Optional[str]) -> Optional[int]:
    """IST hour-of-day (0-23) for an ISO8601 timestamp, or None if the case
    has no scheduled time (e.g. retry_now) or the timestamp is unparseable.

    action_scheduled_for is LLM-emitted with no enforced timezone contract
    (observed in practice as UTC "Z" timestamps) -- never trust the raw hour
    digit. Parse it as an aware datetime (naive values are assumed UTC,
    matching observed LLM output) and convert to IST before reading the
    hour, since NPCI_PEAK_WINDOW_* is an IST-local concept.
    """
    if not action_scheduled_for:
        return None
    try:
        ts = datetime.fromisoformat(action_scheduled_for.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(IST).hour


# --------------------------------------------------------------------------
# Rule conditions + overrides
# --------------------------------------------------------------------------


def _cond_customer_risk_restricted(case: dict, proposed: dict) -> bool:
    return case.get("current_risk_tier") == "restricted"


def _cond_hard_decline(case: dict, proposed: dict) -> bool:
    return case.get("decline_code") in HARD_DECLINE_CODES


def _cond_npci_retry_cap_reached(case: dict, proposed: dict) -> bool:
    return case.get("payment_rail") == "upi_autopay" and case.get("retry_attempt_number", 0) >= NPCI_RETRY_CAP


def _cond_npci_peak_window(case: dict, proposed: dict) -> bool:
    if case.get("payment_rail") != "upi_autopay":
        return False
    hour = _scheduled_hour_ist(proposed.get("action_scheduled_for"))
    return hour is not None and NPCI_PEAK_WINDOW_START_HOUR <= hour < NPCI_PEAK_WINDOW_END_HOUR


def _cond_network_retry_cap_exceeded(case: dict, proposed: dict) -> bool:
    return case.get("cumulative_retries_this_txn", 0) >= NETWORK_RETRY_CAP_THIS_TXN


def _cond_requires_human_review_floor(case: dict, proposed: dict) -> bool:
    off_enum = proposed.get("recommended_action") not in RECOMMENDED_ACTIONS
    low_confidence = proposed.get("confidence", 0.0) < CONFIDENCE_FLOOR
    return off_enum or low_confidence


def _override_force_no_retry(case: dict, proposed: dict) -> dict:
    return {"final_action": NO_RETRY_ACTION}


def _override_escalate_review(case: dict, proposed: dict) -> dict:
    return {"requires_human_review": True}


def _override_force_escalate_human(case: dict, proposed: dict) -> dict:
    # Reuses the existing "escalate_human" RECOMMENDED_ACTIONS value rather
    # than inventing a new final_action -- a guardrail-forced human-review
    # routing is a recommended_action override, same pattern as
    # _override_force_no_retry above, not a new action type.
    return {"final_action": "escalate_human", "requires_human_review": True}


# Ordered rule set -- order determines which rule's override "wins" on
# final_action when several fire on the same case (see apply_guardrails).
#
# Phase 17 -- customer_risk_restricted is listed FIRST, ahead of even
# hard_decline_excluded: a restricted-tier customer must never be offered
# the self-service PTP chat/reschedule flow regardless of what else is true
# about the case, so its override always wins final_action if it fires.
# hard_decline_excluded (and every other rule) still runs and still records
# itself in guardrail_flags when it also fires on the same case -- only the
# WINNING final_action changes, per apply_guardrails' "every rule always
# evaluated" contract -- so the audit trail keeps both facts (e.g. a
# restricted customer's stolen-card case shows both "customer_risk_restricted"
# and "hard_decline_excluded" in guardrail_flags, with final_action forced to
# escalate_human instead of hard_decline_excluded's own no_retry_prompt_update).
GUARDRAIL_RULES: list[GuardrailRule] = [
    ("customer_risk_restricted", _cond_customer_risk_restricted, _override_force_escalate_human),
    ("hard_decline_excluded", _cond_hard_decline, _override_force_no_retry),
    ("npci_retry_cap_reached", _cond_npci_retry_cap_reached, _override_force_no_retry),
    ("npci_peak_window", _cond_npci_peak_window, _override_force_no_retry),
    ("network_retry_cap_exceeded", _cond_network_retry_cap_exceeded, _override_force_no_retry),
    ("requires_human_review_floor", _cond_requires_human_review_floor, _override_escalate_review),
]


def apply_guardrails(case: dict, proposed: dict) -> dict:
    """Run every rule in GUARDRAIL_RULES against (case, proposed) and return
    a NEW dict: everything in `proposed`, plus proposed_action, final_action
    and updated guardrail_flags / requires_human_review.

    Every rule is evaluated regardless of earlier matches. Every rule that
    fires appends its name to guardrail_flags, but only the first rule (in
    list order) whose override sets final_action actually changes it --
    later matching rules still get recorded in guardrail_flags for the
    audit trail.

    guardrail_flags always starts empty here and is populated ONLY by rules
    that fire in this call -- proposed["guardrail_flags"] is deliberately
    NOT carried forward. It can hold upstream labels (e.g. an LLM echoing a
    case_facts guardrail_flags value) that Phase 6 never verified itself;
    seeding from it previously caused duplicate flags whenever an upstream
    label happened to share a name with a rule that also fired here.
    """
    result = dict(proposed)
    result["proposed_action"] = proposed["recommended_action"]
    result["final_action"] = proposed["recommended_action"]
    result["guardrail_flags"] = []
    result["requires_human_review"] = bool(proposed.get("requires_human_review", False))

    final_action_locked = False
    for name, condition, override in GUARDRAIL_RULES:
        if not condition(case, proposed):
            continue
        result["guardrail_flags"].append(name)
        updates = override(case, proposed)
        if "final_action" in updates and not final_action_locked:
            result["final_action"] = updates["final_action"]
            final_action_locked = True
        if updates.get("requires_human_review"):
            result["requires_human_review"] = True

    return result


def run_guardrails_on_batch(cases: list[dict], proposed_list: list[dict]) -> list[dict]:
    """Apply apply_guardrails() pairwise across a batch, ready for the Phase
    7 audit log."""
    return [apply_guardrails(case, proposed) for case, proposed in zip(cases, proposed_list)]


# --------------------------------------------------------------------------
# Phase 13 -- PTP (Promise-to-Pay) guardrail extension.
#
# Same (name, condition, override) rule pattern as GUARDRAIL_RULES above, and
# the same "every rule always runs, guardrail_flags records every rule that
# fired, first rule in list order to set the verdict wins" contract as
# apply_guardrails -- just over a different input shape. These rules run on
# llm_layer.extract_promise_date's output (schema.PromiseDateExtraction:
# extracted_date / confidence / ambiguous / clarification_needed), not on an
# LLMDecision, so they cannot share GUARDRAIL_RULES/apply_guardrails itself
# (there is no recommended_action to override here) -- they live in this
# same module because it's the one place every case's guardrail pass is
# defined, per Phase 13's brief.
#
# pattern_conflict_check (compare a promised date against a customer's
# historical successful-payment day-of-month/time-of-day pattern) is
# deliberately NOT implemented: pipeline/customer_history.py's sqlite store
# only tracks tenure, ltv_tier, historical_ptp_honor_rate (an overall rate,
# not a day/time breakdown) and prior_retry_success_count -- there is no
# per-customer day-of-month or time-of-day success pattern anywhere in this
# codebase to compare against. Building one would mean inventing new state
# for a rule with nothing real to check, so this rule is skipped until that
# data actually exists.
# --------------------------------------------------------------------------

# Reject any promised date more than this many days out.
PTP_WINDOW_CAP_DAYS = 30

# Below this confidence (or ambiguous=true), a promise-date extraction is not
# trusted enough to schedule anything against -- separate constant from
# CONFIDENCE_FLOOR above since that one gates a *recommended_action*, this
# one gates a *date extraction*, and the two thresholds have no reason to
# move together.
PTP_CONFIDENCE_FLOOR = 0.6

# final_action values specific to the PTP pass. NO_RETRY_ACTION is reused
# from Phase 6 above (same real-world meaning: don't schedule anything).
PTP_PROCEED_ACTION = "schedule_ptp_payment_link"
PTP_PENDING_CLARIFICATION_ACTION = "pending_clarification"


def _parse_iso_date(value: Optional[str]) -> Optional[date]:
    """YYYY-MM-DD -> date, or None for missing/unparseable input -- callers
    treat None as "nothing to check" rather than raising, since a rule
    condition must never itself crash the guardrail pass."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _shift_scheduled_time_outside_npci_peak(action_scheduled_for: str) -> str:
    """Shift an ISO8601 timestamp that falls inside the NPCI peak window
    ([10:00, 13:00) IST) to the nearest boundary just outside it, returned as
    a UTC "Z" timestamp (matching the convention observed for LLM-emitted
    timestamps elsewhere in this module). Whichever edge of the window is
    closer to the given hour wins; a tie shifts forward to the end of the
    window."""
    ts = datetime.fromisoformat(action_scheduled_for.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ist = ts.astimezone(IST)

    dist_to_start = ist.hour - NPCI_PEAK_WINDOW_START_HOUR
    dist_to_end = NPCI_PEAK_WINDOW_END_HOUR - ist.hour
    if dist_to_start <= dist_to_end:
        boundary = ist.replace(hour=NPCI_PEAK_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
        adjusted_ist = boundary - timedelta(minutes=1)
    else:
        adjusted_ist = ist.replace(hour=NPCI_PEAK_WINDOW_END_HOUR, minute=0, second=0, microsecond=0)

    return adjusted_ist.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# PTP rule conditions -- each is independently callable/testable, and its
# own name doubles as the guardrail_flags entry when it fires.
# --------------------------------------------------------------------------


def window_cap_check(case: dict, extraction: dict) -> bool:
    extracted = _parse_iso_date(extraction.get("extracted_date"))
    today = _parse_iso_date(case.get("today"))
    if extracted is None or today is None:
        return False
    return (extracted - today).days > PTP_WINDOW_CAP_DAYS


def past_date_check(case: dict, extraction: dict) -> bool:
    extracted = _parse_iso_date(extraction.get("extracted_date"))
    today = _parse_iso_date(case.get("today"))
    if extracted is None or today is None:
        return False
    return extracted < today


def npci_peak_window_check(case: dict, extraction: dict) -> bool:
    """Only meaningful once a concrete execution timestamp exists for the
    promised date -- schema.PromiseDateExtraction itself carries a date, not
    a time, and no later phase yet derives a scheduled time-of-day from it.
    If the caller has already attached one as extraction["action_scheduled_for"]
    (e.g. a future scheduling step), reuse the exact same IST conversion as
    Phase 6's npci_peak_window rule (_scheduled_hour_ist) to check it;
    otherwise there is nothing to check and this rule does not fire."""
    if case.get("payment_rail") != "upi_autopay":
        return False
    hour = _scheduled_hour_ist(extraction.get("action_scheduled_for"))
    return hour is not None and NPCI_PEAK_WINDOW_START_HOUR <= hour < NPCI_PEAK_WINDOW_END_HOUR


def low_confidence_gate(case: dict, extraction: dict) -> bool:
    return extraction.get("confidence", 0.0) < PTP_CONFIDENCE_FLOOR or bool(extraction.get("ambiguous", False))


# --------------------------------------------------------------------------
# PTP rule overrides
# --------------------------------------------------------------------------


def _override_ptp_window_cap(case: dict, extraction: dict) -> dict:
    return {
        "guardrail_status": "rejected_window_cap",
        "final_action": NO_RETRY_ACTION,
        "requires_human_review": True,
    }


def _override_ptp_past_date(case: dict, extraction: dict) -> dict:
    # Same rejection (force final_action to NO_RETRY_ACTION) as window_cap,
    # but a past date is more likely a parsing/timezone slip than a real
    # problem case -- route back to the customer for clarification instead
    # of putting a human in the loop.
    return {
        "guardrail_status": "rejected_past_date",
        "final_action": NO_RETRY_ACTION,
        "routed_to_clarification": True,
    }


def _override_ptp_npci_peak(case: dict, extraction: dict) -> dict:
    original = extraction.get("action_scheduled_for")
    adjusted = _shift_scheduled_time_outside_npci_peak(original)
    return {
        "guardrail_status": "adjusted",
        "original_extracted_date": original,
        "adjusted_date": adjusted,
    }


def _override_ptp_low_confidence(case: dict, extraction: dict) -> dict:
    return {
        "guardrail_status": "pending_clarification",
        "final_action": PTP_PENDING_CLARIFICATION_ACTION,
        "routed_to_clarification": True,
    }


# Order matters exactly as it does for GUARDRAIL_RULES: every rule always
# runs, but only the first rule (in this order) whose override sets
# guardrail_status actually wins the verdict -- see apply_ptp_guardrails.
PTP_GUARDRAIL_RULES: list[GuardrailRule] = [
    ("window_cap_check", window_cap_check, _override_ptp_window_cap),
    ("past_date_check", past_date_check, _override_ptp_past_date),
    ("npci_peak_window_check", npci_peak_window_check, _override_ptp_npci_peak),
    ("low_confidence_gate", low_confidence_gate, _override_ptp_low_confidence),
]


def apply_ptp_guardrails(case: dict, extraction: dict) -> dict:
    """Run every rule in PTP_GUARDRAIL_RULES against (case, extraction) and
    return a NEW dict: everything in `extraction` (the Phase 10 extraction
    output -- extracted_date / confidence / ambiguous / clarification_needed
    / model_version / timestamp -- preserved verbatim, never overwritten, so
    the original LLM output and the guardrail's verdict are always two
    separate, inspectable fields -- the same proposed_action/final_action
    split apply_guardrails uses above), plus:

      guardrail_status      -- "approved" (no rule fired), or one of
                                "rejected_window_cap", "rejected_past_date",
                                "adjusted", "pending_clarification" set by
                                whichever rule won.
      final_action          -- PTP_PROCEED_ACTION by default; forced to
                                NO_RETRY_ACTION or PTP_PENDING_CLARIFICATION_ACTION
                                by whichever rule wins the verdict.
      guardrail_flags       -- names of every rule that fired, in the order
                                they were checked (empty means "approved").
      requires_human_review -- true only for window_cap_check (a real
                                problem case); never set by past_date_check
                                or low_confidence_gate, which route to the
                                clarification flow instead (see
                                routed_to_clarification).
      routed_to_clarification -- true when this case should be re-asked of
                                the customer rather than escalated to a human
                                or scheduled.
      original_extracted_date / adjusted_date -- both always present (None
                                unless npci_peak_window_check fires) so the
                                audit row has a stable shape; populated
                                together when a scheduled time is shifted out
                                of the NPCI peak window.

    case must include "today" (ISO date) -- same explicit-today contract
    llm_layer.extract_promise_date already requires of its case_context, so
    in practice this is the same dict. payment_rail is read when present
    (npci_peak_window_check); its absence just means that rule never fires.
    """
    if not case.get("today"):
        raise ValueError(
            "case['today'] is required -- window_cap_check/past_date_check need an explicit "
            "'today' to compare against, same contract as llm_layer.extract_promise_date's case_context."
        )

    result = dict(extraction)
    result["guardrail_status"] = "approved"
    result["final_action"] = PTP_PROCEED_ACTION
    result["guardrail_flags"] = []
    result["requires_human_review"] = False
    result["routed_to_clarification"] = False
    result["original_extracted_date"] = None
    result["adjusted_date"] = None

    status_locked = False
    for name, condition, override in PTP_GUARDRAIL_RULES:
        if not condition(case, extraction):
            continue
        result["guardrail_flags"].append(name)
        updates = override(case, extraction)
        if "guardrail_status" in updates and not status_locked:
            result["guardrail_status"] = updates["guardrail_status"]
            if "final_action" in updates:
                result["final_action"] = updates["final_action"]
            status_locked = True
        if updates.get("requires_human_review"):
            result["requires_human_review"] = True
        if updates.get("routed_to_clarification"):
            result["routed_to_clarification"] = True
        if "original_extracted_date" in updates:
            result["original_extracted_date"] = updates["original_extracted_date"]
        if "adjusted_date" in updates:
            result["adjusted_date"] = updates["adjusted_date"]

    return result


def run_ptp_guardrails_on_batch(cases: list[dict], extractions: list[dict]) -> list[dict]:
    """Apply apply_ptp_guardrails() pairwise across a batch -- same shape as
    run_guardrails_on_batch above."""
    return [apply_ptp_guardrails(case, extraction) for case, extraction in zip(cases, extractions)]
