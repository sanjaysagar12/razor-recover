"""
Phase 4 tests -- Confidence Gate + SHAP.

Run with:
    python -m pytest tests/test_phase4.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import confidence_gate, shap_extract
from scripts import run_phase4

ROUTED_PCT_MIN = 5.0
ROUTED_PCT_MAX = 50.0


# --------------------------------------------------------------------------
# SHAP additivity
# --------------------------------------------------------------------------
def test_shap_additivity_on_sample_rows():
    ctx = shap_extract._get_context()
    n_rows = ctx.Xt.shape[0]
    rng = np.random.default_rng(123)  # different seed than the module's own internal check
    sample_idx = rng.choice(n_rows, size=min(10, n_rows), replace=False)

    for idx in sample_idx:
        reconstructed = ctx.total_from_shap(int(idx))
        actual = float(ctx.predicted_proba[idx])
        assert abs(reconstructed - actual) <= shap_extract.SANITY_CHECK_TOLERANCE, (
            f"case_id={ctx.df.iloc[idx]['case_id']}: base_value+sum(shap)={reconstructed:.8f} "
            f"vs predict_proba={actual:.8f}"
        )


# --------------------------------------------------------------------------
# Routing distribution is not degenerate, on the real synthetic batch.
# The percentile-based band should land this comfortably inside [5%, 50%]
# regardless of how compressed the underlying model's score distribution is
# -- no special-casing needed here now.
# --------------------------------------------------------------------------
def test_routing_distribution_not_degenerate():
    scores_df = shap_extract.get_scores_df()
    result = confidence_gate.run_full_batch(scores_df)
    records = result["records"]

    total = len(records)
    routed = sum(1 for r in records if r["routed_to_llm"])
    routed_pct = 100.0 * routed / total

    assert ROUTED_PCT_MIN <= routed_pct <= ROUTED_PCT_MAX, (
        f"Routing distribution looks degenerate: {routed}/{total} cases "
        f"({routed_pct:.1f}%) routed to the LLM, outside the expected "
        f"[{ROUTED_PCT_MIN}%, {ROUTED_PCT_MAX}%] band (computed band: "
        f"{result['computed_probability_band']}). This likely means "
        f"PROB_BAND_LOWER_PCTILE/PROB_BAND_UPPER_PCTILE or the synthetic "
        f"data's score distribution need adjusting, not this test."
    )


# --------------------------------------------------------------------------
# Ambiguous decline code overrides a confident probability
# --------------------------------------------------------------------------
def test_ambiguous_code_overrides_high_confidence_probability():
    result = confidence_gate.route_case(
        "synthetic_case_ambiguous", 0.95, "05_do_not_honor", band_low=0.3, band_high=0.6
    )

    assert result["routed_to_llm"] is True
    assert "ambiguous_code" in result["routing_trigger"]
    assert "probability_band" not in result["routing_trigger"]
    assert result["template_action"] is None


# --------------------------------------------------------------------------
# Probability-band boundaries are inclusive (band bounds are now dynamic
# inputs to route_case rather than module constants, so we just pick a
# representative band here).
# --------------------------------------------------------------------------
@pytest.mark.parametrize("boundary_score", [0.4, 0.6])
def test_probability_band_boundary_is_inclusive(boundary_score):
    result = confidence_gate.route_case(
        "synthetic_case_boundary", boundary_score, "issuer_unavailable", band_low=0.4, band_high=0.6
    )

    assert result["routed_to_llm"] is True
    assert "probability_band" in result["routing_trigger"]
    assert result["template_action"] is None


# --------------------------------------------------------------------------
# The ambiguous_code trigger's rows must not skew the percentile fit used
# for the probability_band trigger.
# --------------------------------------------------------------------------
def test_percentile_band_excludes_ambiguous_code_rows():
    non_ambiguous_scores = [0.10, 0.20, 0.30, 0.40, 0.50]
    ambiguous_scores = [0.95, 0.96, 0.97]  # outliers that would drag percentiles up if included

    scores_df = pd.DataFrame(
        {
            "case_id": [f"case_{i}" for i in range(len(non_ambiguous_scores) + len(ambiguous_scores))],
            "tree_model_score": non_ambiguous_scores + ambiguous_scores,
            "decline_code": (["51_insufficient_funds"] * len(non_ambiguous_scores))
            + (["05_do_not_honor"] * len(ambiguous_scores)),
        }
    )

    expected_low, expected_high = np.percentile(
        non_ambiguous_scores, [confidence_gate.PROB_BAND_LOWER_PCTILE, confidence_gate.PROB_BAND_UPPER_PCTILE]
    )
    inclusive_low, inclusive_high = np.percentile(
        non_ambiguous_scores + ambiguous_scores,
        [confidence_gate.PROB_BAND_LOWER_PCTILE, confidence_gate.PROB_BAND_UPPER_PCTILE],
    )
    # Sanity check that this fixture actually exercises the exclusion --
    # if these were equal the test wouldn't be able to tell the two apart.
    assert (expected_low, expected_high) != (inclusive_low, inclusive_high)

    band = confidence_gate.compute_probability_band(scores_df)

    assert band == pytest.approx((expected_low, expected_high))
    assert band != pytest.approx((inclusive_low, inclusive_high))


# --------------------------------------------------------------------------
# Reproducibility
# --------------------------------------------------------------------------
def test_confidence_gate_run_full_batch_is_reproducible():
    scores_df = shap_extract.get_scores_df()
    first = confidence_gate.run_full_batch(scores_df)
    second = confidence_gate.run_full_batch(scores_df)
    assert first == second


def test_computed_probability_band_is_reproducible():
    scores_df = shap_extract.get_scores_df()
    first = confidence_gate.run_full_batch(scores_df)["computed_probability_band"]
    second = confidence_gate.run_full_batch(scores_df)["computed_probability_band"]
    assert first == second


def test_shap_extract_run_full_batch_is_reproducible():
    first = shap_extract.run_full_batch()
    second = shap_extract.run_full_batch()
    assert first == second


# --------------------------------------------------------------------------
# No case silently dropped
# --------------------------------------------------------------------------
def test_every_input_case_id_present_in_phase4_output():
    input_case_ids = set(shap_extract.load_batch_df()["case_id"])
    combined_records = run_phase4.main()
    output_case_ids = {r["case_id"] for r in combined_records}

    missing = input_case_ids - output_case_ids
    assert not missing, f"{len(missing)} case_id(s) dropped from Phase 4 output: {sorted(missing)[:10]}"
    assert input_case_ids == output_case_ids
