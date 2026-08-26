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


def build_user_prompt(case: dict) -> str:
    """case: {case_id, tree_model_score, shap_top_features, case_facts} --
    see pipeline/run_phase5.py for how this is assembled from the Phase 4
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
