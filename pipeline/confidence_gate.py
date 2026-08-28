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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

from guardrails import HARD_DECLINE_CODES, NO_RETRY_ACTION

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


def _is_hard_decline(decline_code: str) -> bool:
    return decline_code in HARD_DECLINE_CODES


def _template_action(tree_probability: float, decline_code: str, band_high: float) -> str:
    """Template action for a case NOT routed to the LLM (see route_case).

    Branches on decline_code type as well as score:
      * A hard decline never gets a retry default regardless of score --
        guardrails.py's hard_decline_excluded rule would also catch this
        downstream, but the template shouldn't rely on guardrails to
        correct an obviously wrong default (see guardrails.HARD_DECLINE_CODES,
        the same "CLEAR_HARD" bucket used there).
      * band_low/band_high is a RELATIVE percentile slice of this batch's
        own (compressed) score distribution, not an absolute floor -- a
        non-hard decline scoring below it is not necessarily hopeless, so
        it gets a lower-urgency retry_scheduled instead of being written
        off outright (BEFORE this fix: any non-ambiguous-code case below
        band_high, hard or not, was written off as no_retry_prompt_update
        purely from score -- see PHASE7_REPORT.md for the leaked-revenue
        this caused on soft-decline codes with a below-band score).
    """
    if _is_hard_decline(decline_code):
        return NO_RETRY_ACTION
    if tree_probability > band_high:
        return "retry_now"
    return "retry_scheduled"


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


def route_case(
    case_id: str,
    tree_probability: float,
    decline_code: str,
    band_low: float,
    band_high: float,
    is_ambiguous: bool | None = None,
) -> dict:
    """is_ambiguous: override for the ambiguous_code trigger, supplied by a
    caller that has a better source of truth than the synthetic-taxonomy
    prefix match below -- e.g. webhook_receiver.py passes
    pipeline.decline_code_mapper's bucket-based is_ambiguous for real
    Razorpay decline codes, which never match AMBIGUOUS_DECLINE_CODES'
    synthetic prefixes ("05", ...) in the first place. None (the default)
    preserves the original prefix-match behavior exactly -- run_full_batch
    below never passes this, so the synthetic batch simulation is
    byte-for-byte unaffected by this parameter's existence."""
    routing_trigger: list[str] = []
    if _in_probability_band(tree_probability, band_low, band_high):
        routing_trigger.append("probability_band")
    ambiguous_code_flag = is_ambiguous if is_ambiguous is not None else _is_ambiguous_code(decline_code)
    if ambiguous_code_flag:
        routing_trigger.append("ambiguous_code")

    routed_to_llm = len(routing_trigger) > 0

    template_action = None if routed_to_llm else _template_action(tree_probability, decline_code, band_high)

    return {
        "case_id": case_id,
        "tree_model_score": float(tree_probability),
        "routed_to_llm": routed_to_llm,
        "routing_trigger": routing_trigger,
        # The actual boolean that decided whether "ambiguous_code" landed in
        # routing_trigger above -- exposed explicitly so callers building a
        # human-readable rationale (run_batch._routing_rationale) can read
        # the real decision instead of recomputing it via _is_ambiguous_code,
        # which silently ignores the is_ambiguous override and reintroduces
        # the legacy prefix-match result into the explanation text.
        "ambiguous_code_flag": ambiguous_code_flag,
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
