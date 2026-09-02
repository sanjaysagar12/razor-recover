"""
Promise-to-pay OFFER-ELIGIBILITY gate.

Decides whether a case should even be OFFERED a PTP conversation, before
any chat UI would ever render one. Deliberately separate from
pipeline/guardrails.py's two existing rule passes:

  GUARDRAIL_RULES     -- decides what retry/schedule ACTION the system
                         itself should take (retry_now / escalate_human /
                         no_retry_prompt_update / ...).
  PTP_GUARDRAIL_RULES  -- validates a date the CUSTOMER already supplied
                         (extracted_date/confidence/ambiguous) before
                         scheduling a payment link against it.

Both of those act on a decision that already exists (the pipeline's own
proposed action, or the customer's own reply). should_offer_ptp() answers a
prior, narrower question -- should this case even be OFFERED a chance to
make a promise at all -- which has nothing to do with a final_action or a
date extraction's validity, so it doesn't belong in either existing rule
list. Lives in its own module for the same reason customer_ptp_stats.py and
ptp_outcomes.py are their own modules rather than folded into guardrails.py
-- one clearly-named concern per file.

Out of scope here (separate, already-scoped work): the chat reply endpoint
(webhook_receiver.api_promise_reply), the LLM date-extraction call
(llm_layer.py), payment-link creation (execute_action.py), and honor/break
tracking (pipeline/ptp_outcomes.py). This module only decides whether a
case is even eligible for that whole flow to begin -- it never calls into
any of them.

Smoke test:
    python tests/test_ptp_trigger.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import decline_code_mapper
import guardrails
import promise_store
from customer_ptp_stats import RISK_TIER_RESTRICTED, get_risk_tier

# trigger_category values -- exact strings, part of the audit-log/dashboard
# contract (see webhook_receiver.py's ptp_trigger_category audit column and
# dashboard/index.html's badge-label lookup).
CATEGORY_HARD_DECLINE = "hard_decline"
CATEGORY_OPEN_PROMISE_EXISTS = "open_promise_exists"
CATEGORY_RESTRICTED_TIER = "restricted_tier"
CATEGORY_FIRST_FAILURE_AWAITING_AUTO_RETRY = "first_failure_awaiting_auto_retry"
CATEGORY_HIGH_LTV_FIRST_FAILURE = "high_ltv_first_failure"
CATEGORY_INSUFFICIENT_FUNDS_CODE = "insufficient_funds_code"
CATEGORY_APPROACHING_RETRY_CAP = "approaching_retry_cap"
CATEGORY_RETRY_FAILED_ONCE = "retry_failed_once"

# Synthetic-vocabulary decline codes (see webhook_receiver.
# RAZORPAY_REASON_TO_DECLINE_CODE -- decline_code stays in this vocabulary
# for every case, real or manual-test) that specifically imply a funds/
# timing problem the CUSTOMER is the only real source of information about
# -- distinct from a payment-instrument problem (hard decline, handled
# separately below via decline_code_mapper) or a generic/ambiguous one.
FUNDS_TIMING_DECLINE_CODES = {"insufficient_funds", "51_insufficient_funds"}

HIGH_LTV_TIER = "high"


def _decline_code_bucket(case: dict) -> str:
    """CLEAR_HARD / CLEAR_SOFT / AMBIGUOUS for this case, via
    decline_code_mapper.py. Reuses case['decline_code_bucket'] when a caller
    has already computed it -- every real webhook case does, via
    webhook_receiver.map_payload_to_case's own call into
    decline_code_mapper.map_razorpay_error_reason, and every manual-test/
    preset case sets it directly (see webhook_receiver.DEFAULT_TEST_CASE) --
    otherwise derives it directly from case['decline_code'] so this
    function also works against a bare case dict (e.g. in tests) without
    requiring the field to be precomputed."""
    bucket = case.get("decline_code_bucket")
    if bucket is not None:
        return bucket
    return decline_code_mapper.map_razorpay_error_reason(case.get("decline_code") or "")["decline_code_bucket"]


def _is_approaching_retry_cap(case: dict) -> bool:
    """True on the LAST attempt before the existing retry-cap guardrail
    rules (guardrails.NPCI_RETRY_CAP for upi_autopay,
    guardrails.NETWORK_RETRY_CAP_THIS_TXN otherwise) would force
    final_action to no_retry_prompt_update -- reuses those exact constants
    rather than duplicating the cap value, so the two stay in sync if
    either guardrail threshold ever changes."""
    if case.get("payment_rail") == "upi_autopay":
        return case.get("retry_attempt_number", 1) == guardrails.NPCI_RETRY_CAP - 1
    cumulative = case.get("cumulative_retries_this_txn", case.get("retry_attempt_number", 1))
    return cumulative == guardrails.NETWORK_RETRY_CAP_THIS_TXN - 1


def should_offer_ptp(case: dict) -> dict:
    """Decides whether `case` should be offered a promise-to-pay
    conversation. Expects at minimum decline_code, retry_attempt_number,
    customer_id, and ltv_tier (if available) -- i.e. any case dict shaped
    like webhook_receiver.map_payload_to_case()'s output; missing fields
    degrade gracefully (see individual field reads below) rather than
    raising, since a case not yet enriched with every optional field must
    still get a defensible answer, not a crash.

    Returns {"offer_ptp": bool, "trigger_category": str, "reason": str} --
    never just a bool, so the reasoning survives into the audit log and the
    dashboard, not only the outcome.

    Evaluation order (first match wins -- a case can genuinely satisfy more
    than one condition, e.g. a high-LTV customer on an insufficient_funds
    first failure; this returns the single most specific/authoritative
    match, not every match):

      1. restricted_tier         (False, absolute veto) -- checked FIRST,
         same precedent guardrails.GUARDRAIL_RULES already sets for
         customer_risk_restricted (listed ahead of even hard_decline).
      2. hard_decline             (False, absolute veto)
      3. open_promise_exists      (False, absolute veto)
      4. high_ltv_first_failure   (True)
      5. insufficient_funds_code  (True, independent of retry count)
      6. approaching_retry_cap    (True)
      7. first_failure_awaiting_auto_retry (False) -- only reached once
         retry_attempt_number <= 1 and none of the True triggers above
         fired; an AMBIGUOUS-bucket first failure is treated the same as a
         CLEAR_SOFT one here (see decline_code_bucket note below) since the
         spec this was built against never addresses ambiguous codes
         explicitly, and "wait for the silent auto-retry" is the safer
         default either way.
      8. retry_failed_once        (True) -- the fallback for everything
         past a first failure that didn't hit a more specific True trigger
         above; covers retry_attempt_number 2 and beyond, not just exactly
         2, since more failed attempts is never LESS of a reason to ask the
         customer directly.

    decline_code_bucket is only ever CLEAR_SOFT or AMBIGUOUS by the time
    step 4 onward runs -- CLEAR_HARD already returned at step 2.
    """
    customer_id = case.get("customer_id")
    ltv_tier = case.get("ltv_tier")
    retry_attempt_number = case.get("retry_attempt_number", 1)

    if get_risk_tier(customer_id) == RISK_TIER_RESTRICTED:
        return {
            "offer_ptp": False,
            "trigger_category": CATEGORY_RESTRICTED_TIER,
            "reason": "Customer is on the restricted risk tier -- routes straight to human review, not self-service PTP.",
        }

    if _decline_code_bucket(case) == decline_code_mapper.CLEAR_HARD:
        return {
            "offer_ptp": False,
            "trigger_category": CATEGORY_HARD_DECLINE,
            "reason": "Decline code is a hard decline -- the payment method itself won't work, so a date commitment is meaningless.",
        }

    if promise_store.has_open_promise(customer_id):
        return {
            "offer_ptp": False,
            "trigger_category": CATEGORY_OPEN_PROMISE_EXISTS,
            "reason": "Customer already has an open, unresolved promise -- never stack a second PTP prompt on top of it.",
        }

    if ltv_tier == HIGH_LTV_TIER and retry_attempt_number <= 1:
        return {
            "offer_ptp": True,
            "trigger_category": CATEGORY_HIGH_LTV_FIRST_FAILURE,
            "reason": "High-LTV customer -- stakes justify going straight to direct contact even on a first failure.",
        }

    if case.get("decline_code") in FUNDS_TIMING_DECLINE_CODES:
        return {
            "offer_ptp": True,
            "trigger_category": CATEGORY_INSUFFICIENT_FUNDS_CODE,
            "reason": "Decline code implies a funds/timing issue -- the customer is the only real source of that information.",
        }

    if _is_approaching_retry_cap(case):
        return {
            "offer_ptp": True,
            "trigger_category": CATEGORY_APPROACHING_RETRY_CAP,
            "reason": "Retry attempts are approaching the network/NPCI cap -- last real chance before a forced no-retry outcome.",
        }

    if retry_attempt_number <= 1:
        return {
            "offer_ptp": False,
            "trigger_category": CATEGORY_FIRST_FAILURE_AWAITING_AUTO_RETRY,
            "reason": "First failure on a non-hard decline code -- let the automated retry run before asking the customer anything.",
        }

    return {
        "offer_ptp": True,
        "trigger_category": CATEGORY_RETRY_FAILED_ONCE,
        "reason": "An automated retry has already failed on this decline code -- time to ask the customer directly.",
    }
