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

Smoke test:
    python pipeline/test_guardrails.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
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


# Ordered rule set -- order determines which rule's override "wins" on
# final_action when several fire on the same case (see apply_guardrails).
GUARDRAIL_RULES: list[GuardrailRule] = [
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
