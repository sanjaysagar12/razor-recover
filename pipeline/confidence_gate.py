"""
Phase 4 -- Confidence Gate.

Decides, per case, whether the tree model's score (Phase 3) is trustworthy
enough to act on directly with a template action, or whether the case needs
to be routed to the LLM layer (Phase 5+) for reasoning over the SHAP
explanation (Phase 4's shap_extract.py).

A case is routed to the LLM if EITHER trigger fires:
  1. probability_band -- the tree model's score falls in the uncertain
     middle slice of THIS BATCH's score distribution, where "recover" vs.
     "don't recover" is a near-coin-flip relative to everything else scored
     and a template action would be acting on noise.

     This band is computed at runtime as a percentile slice
     (PROB_BAND_LOWER_PCTILE..PROB_BAND_UPPER_PCTILE) of the batch's own
     scores, not a fixed absolute probability range. A fixed range doesn't
     work here: the primary model is logistic regression, and its predicted
     probabilities are naturally compressed (calibration bins span roughly
     0.06-0.71 on this dataset, per models/model_report.md) -- a fixed
     [0.35, 0.65] band swallowed 60% of a real batch regardless of whether
     those cases were actually ambiguous relative to each other. A
     percentile band always captures the same *relative* slice of
     uncertainty no matter how compressed or spread out the model's scores
     are.
  2. ambiguous_code -- the decline code is domain-flagged as ambiguous
     regardless of what the score says (see AMBIGUOUS_DECLINE_CODES below).
     A confident-looking score on an ambiguous code is not trusted, because
     the model was trained on too little data per code to be confident
     about exactly *why* it's confident here.

Rows already caught by the ambiguous_code trigger are excluded when fitting
the probability-band percentiles (but not from having the band applied to
them) -- otherwise a cluster of ambiguous-code scores could skew the
percentile cutoffs used for every other, non-ambiguous case.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Probability band, expressed as PERCENTILES of this batch's own score
# distribution (not fixed probability cutoffs -- see module docstring for
# why an absolute band doesn't work for a compressed-probability model).
# 42.5..57.5 is a 15-percentile-wide middle slice.
PROB_BAND_LOWER_PCTILE = 42.5
PROB_BAND_UPPER_PCTILE = 57.5

# Decline-code prefixes (the numeric code before the descriptive suffix,
# e.g. "05_do_not_honor" -> "05") that are domain-flagged as ambiguous even
# when the tree model's score looks confident. "05" = "Do Not Honor": a
# soft-looking generic decline that issuers use for everything from
# temporary risk holds to permanent blocks -- blind-retrying it can trigger
# issuer-side fraud flags, so it always gets a human/LLM-reasoned look
# rather than an automatic retry. Extend this set as more such codes are
# identified.
AMBIGUOUS_DECLINE_CODES = {"05"}


def _decline_code_prefix(decline_code: str) -> str:
    return str(decline_code).split("_", 1)[0]


def _is_ambiguous_code(decline_code: str) -> bool:
    return _decline_code_prefix(decline_code) in AMBIGUOUS_DECLINE_CODES


def _in_probability_band(tree_probability: float, band_low: float, band_high: float) -> bool:
    return band_low <= tree_probability <= band_high


def compute_probability_band(scores_df: pd.DataFrame) -> tuple[float, float]:
    """Percentile band fit on the scores of rows NOT already flagged by the
    ambiguous_code trigger, so that cluster doesn't skew the cutoffs used
    for everyone else. Falls back to the full batch if every row happens to
    be ambiguous-flagged (band would otherwise be undefined)."""
    ambiguous_mask = scores_df["decline_code"].apply(_is_ambiguous_code)
    non_ambiguous_scores = scores_df.loc[~ambiguous_mask, "tree_model_score"]

    fit_scores = non_ambiguous_scores if len(non_ambiguous_scores) > 0 else scores_df["tree_model_score"]

    low, high = np.percentile(fit_scores.to_numpy(), [PROB_BAND_LOWER_PCTILE, PROB_BAND_UPPER_PCTILE])
    return float(low), float(high)


def route_case(case_id: str, tree_probability: float, decline_code: str, band_low: float, band_high: float) -> dict:
    routing_trigger: list[str] = []
    if _in_probability_band(tree_probability, band_low, band_high):
        routing_trigger.append("probability_band")
    if _is_ambiguous_code(decline_code):
        routing_trigger.append("ambiguous_code")

    routed_to_llm = len(routing_trigger) > 0

    if routed_to_llm:
        template_action = None
    elif tree_probability > band_high:
        template_action = "retry_now"
    else:
        template_action = "no_retry_prompt_update"

    return {
        "case_id": case_id,
        "tree_model_score": float(tree_probability),
        "routed_to_llm": routed_to_llm,
        "routing_trigger": routing_trigger,
        "template_action": template_action,
    }


def run_full_batch(scores_df: pd.DataFrame) -> dict:
    """scores_df must have columns: case_id, tree_model_score, decline_code.

    Returns {"computed_probability_band": {"low": ..., "high": ...},
    "records": [...]} -- the band is computed fresh from this batch's score
    distribution every call (see compute_probability_band), so it's
    surfaced here rather than silently discarded.
    """
    band_low, band_high = compute_probability_band(scores_df)
    records = [
        route_case(row.case_id, row.tree_model_score, row.decline_code, band_low, band_high)
        for row in scores_df.itertuples(index=False)
    ]
    return {
        "computed_probability_band": {"low": band_low, "high": band_high},
        "records": records,
    }
