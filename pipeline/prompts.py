"""
Phase 5 -- shared system prompt + few-shot examples + user-prompt assembly.

Single source of truth for prompt text so ClaudeAdapter and GeminiAdapter
(pipeline/llm_layer.py) are tested against literally the same instructions.
"""

from __future__ import annotations

import json

SYSTEM_PROMPT = """You are the reasoning layer of a payment-recovery decision pipeline. A tree-based model has already scored this case and flagged it as ambiguous (mid-band probability or an ambiguous decline code) -- your job is to recommend a recovery action by reasoning ONLY over the facts you are given.

Rules:
1. Reason ONLY from the "shap_top_features" list provided for this case. Never introduce a factor, statistic, or feature that is not present in that list as the basis for your recommendation.
2. Before writing reasoning_summary, first list out the exact feature names you were given in shap_top_features -- this is a discipline step to keep you from drifting to features you were not given.
3. Every time you reference a SHAP feature's value, state its sign explicitly and get the direction right: a NEGATIVE shap_value pushed the case toward non-recovery/decline, a POSITIVE shap_value pushed it toward recovery. Never describe a negative shap_value as positive/favorable, or a positive shap_value as negative/unfavorable.
4. Do not cite any case_facts field as justification for recommended_action unless that same field also appears in shap_top_features for this case. You may mention a case_facts field that is NOT in shap_top_features, but only as "unranked context, not a SHAP-attributed driver" -- never as a reason the action was chosen.
5. If a data point you would need to be confident about is missing from what you were given, say so explicitly in reasoning_summary rather than guessing.
6. Work out your reasoning first, then commit to recommended_action -- reasoning_summary should read as though it were written before the action was chosen, not as a post-hoc justification.
7. recommended_action must be exactly one of: retry_now, retry_scheduled, no_retry_prompt_update, escalate_human, prompt_alt_payment.
8. Set confidence to your genuine calibrated confidence in the recommended action, between 0 and 1 -- do not default to a fixed value.
9. Set requires_human_review to true if you are not confident enough for this case to be actioned automatically, even if you still give your best-guess recommended_action.
10. Set action_scheduled_for to an ISO8601 timestamp only when recommended_action is retry_scheduled; otherwise leave it null.

Submit your decision using the structured output format -- do not respond with free text."""


# Placeholder few-shot pairs -- refine with real PTP honor/break patterns
# from the domain doc before the demo. Each maps a SHAP profile + case
# facts to the action that turned out correct historically.
FEW_SHOT_EXAMPLES = [
    {
        "shap_profile": [
            {"feature": "historical_ptp_honor_rate", "shap_value": 0.31},
            {"feature": "ltv_tier", "shap_value": 0.18},
            {"feature": "issuer_bank_risk_tier", "shap_value": -0.05},
        ],
        "case_facts": {
            "decline_code": "05_do_not_honor",
            "ltv_tier": "high",
            "historical_ptp_honor_rate": 0.82,
            "retry_attempt_number": 1,
            "issuer_bank_risk_tier": "low_risk",
        },
        "correct_past_action": "retry_scheduled",
    },
    {
        "shap_profile": [
            {"feature": "historical_ptp_honor_rate", "shap_value": -0.29},
            {"feature": "prior_retry_success_count", "shap_value": -0.12},
            {"feature": "amount_vs_historical_avg", "shap_value": -0.09},
        ],
        "case_facts": {
            "decline_code": "05_do_not_honor",
            "ltv_tier": "low",
            "historical_ptp_honor_rate": 0.21,
            "retry_attempt_number": 4,
            "prior_retry_success_count": 0,
        },
        "correct_past_action": "escalate_human",
    },
    {
        "shap_profile": [
            {"feature": "issuer_bank_risk_tier", "shap_value": -0.22},
            {"feature": "payment_rail", "shap_value": -0.10},
            {"feature": "is_peak_execution_window", "shap_value": 0.06},
        ],
        "case_facts": {
            "decline_code": "issuer_unavailable",
            "payment_rail": "upi_autopay",
            "issuer_bank_risk_tier": "high_risk",
            "retry_attempt_number": 2,
        },
        "correct_past_action": "retry_scheduled",
    },
]


def _format_few_shot() -> str:
    blocks = []
    for i, ex in enumerate(FEW_SHOT_EXAMPLES, start=1):
        blocks.append(
            f"Example {i}:\n"
            f"  shap_profile: {json.dumps(ex['shap_profile'])}\n"
            f"  case_facts: {json.dumps(ex['case_facts'])}\n"
            f"  correct_past_action: {ex['correct_past_action']}"
        )
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------
# Promise-to-pay date extraction -- parses a customer's free-text reply
# (collected via /api/promise-reply) into a structured commitment date.
# Separate system prompt from SYSTEM_PROMPT above: different task (NL date
# extraction, not a SHAP-grounded recovery-action recommendation), same
# adapters/client/validation machinery in pipeline/llm_layer.py.
# --------------------------------------------------------------------------
PROMISE_DATE_SYSTEM_PROMPT = """You extract a promise-to-pay date from a customer's free-text reply in a payment-recovery chat. You are given the actual current date explicitly -- never infer or assume "today" on your own, and resolve every relative expression ("next Friday", "in 3 days", "end of the month", "give me a week") against that given date, not any other date.

Rules:
1. The current date is always given to you in the prompt as "today's date". Treat it as ground truth. Resolve all relative date expressions against it.
2. If the reply contains an extractable commitment -- a specific date, or a relative expression precise enough to resolve to one exact date -- set extracted_date to that date in YYYY-MM-DD format.
3. If the reply does NOT contain an extractable commitment (no date mentioned, or a vague expression with no further specificity such as "soon", "later", "this month", "maybe next month"), set extracted_date to null and ambiguous to true. Do not guess a date to fill the field.
4. Never invent a date that is not implied by the text. A vague-but-present time reference ("sometime next week") is ambiguous unless it resolves to one exact day -- when it does not resolve to a single day, extracted_date must be null. "End of the month" / "end of this month" is an exception worth naming explicitly: it DOES resolve to one exact day -- the last calendar day of the month containing today's date -- so treat it as extractable, not vague, and compute that day from today's date. Contrast this with "this month" or "sometime this month" alone (no "end of"), which name a whole month with no single day implied and stay ambiguous.
5. A reply with no commitment at all (e.g. "I'm broke right now", "can't afford it", refusal, or an unrelated/hostile message) is also ambiguous=true, extracted_date=null.
6. Whenever ambiguous is true, populate clarification_needed with a short, ready-to-send, customer-facing follow-up question (e.g. "Could you give me a specific date you'd like to pay by?"). It will be sent to the customer as-is, so phrase it as a direct, polite question -- not an internal note. When ambiguous is false, clarification_needed must be null.
7. Set confidence to reflect your certainty in the EXTRACTION, not the customer's tone or sentiment. A clearly stated date is high confidence (>=0.85) even if the customer sounds annoyed or frustrated. A vague-but-present relative date that still resolves to one exact day (e.g. "give me a week") is medium-high confidence (roughly 0.55-0.8). No extractable date at all is confidence near 0 (<=0.15), together with ambiguous=true.
8. Submit your result using the structured output format -- do not respond with free text."""


def build_promise_date_user_prompt(message: str, case_context: dict) -> str:
    """case_context must include "today" (ISO date, explicit -- see
    PROMISE_DATE_SYSTEM_PROMPT rule 1). case_id/amount/decline_code are
    included when present for grounding context only -- optional, never
    required for extraction itself."""
    facts = [f'today\'s date: {case_context["today"]}']
    if case_context.get("case_id"):
        facts.append(f"case_id: {case_context['case_id']}")
    if case_context.get("amount") is not None:
        facts.append(f"original amount due: {case_context['amount']}")
    if case_context.get("decline_code"):
        facts.append(f"decline_code: {case_context['decline_code']}")
    return (
        f"{chr(10).join(facts)}\n\n"
        f'Customer\'s reply:\n"""\n{message}\n"""\n\n'
        "Extract the promise-to-pay date from this reply."
    )


def build_user_prompt(case: dict) -> str:
    """case: {case_id, tree_model_score, shap_top_features, case_facts} --
    see scripts/run_phase5.py for how this is assembled from the Phase 4
    output plus the raw case facts (shap_extract.get_case_facts)."""
    shap_for_prompt = [
        {"feature": f["feature"], "shap_value": f["shap_value"]} for f in case["shap_top_features"]
    ]
    return (
        "Here are past examples of correctly-resolved cases, for pattern reference only "
        "-- do not copy their action, reason from THIS case's own facts:\n\n"
        f"{_format_few_shot()}\n\n"
        "Now decide this case:\n\n"
        f"case_id: {case['case_id']}\n"
        f"tree_model_score: {case['tree_model_score']}\n"
        f"shap_top_features: {json.dumps(shap_for_prompt)}\n"
        f"case_facts: {json.dumps(case['case_facts'])}\n"
    )
