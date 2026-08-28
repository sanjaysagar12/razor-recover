"""
Phase 4 -- SHAP Extraction Layer.

Loads the PRIMARY_MODEL selected by Phase 3 (models/artifacts/model_metadata.json),
computes exact SHAP attributions for every case in the batch, and exposes the
top-5 contributing features per case for the LLM layer (Phase 5+) to reason
over.

--------------------------------------------------------------------------
Data-source note (same adaptation Phase 3 made -- see models/train_tree_models.py)
--------------------------------------------------------------------------
The Phase 4 spec assumes a single `data/synthetic_batch.csv`. The actual
Phase 2 output in this repo is `data/train.csv` + `data/holdout.csv`. We
reuse Phase 3's own adaptation exactly: concatenate the two files back into
one 280-row pool (the "batch") and use `case_id` as the row key, so the
SHAP explanations line up 1:1 with the same rows Phase 3 scored.

--------------------------------------------------------------------------
Explainer choice
--------------------------------------------------------------------------
* Logistic regression -> shap.LinearExplainer. This is exact (closed-form)
  for a linear model, but it explains the model's raw linear score (log-odds),
  not probability. We sigmoid-transform base_value + sum(shap_values) before
  comparing to predict_proba in the additivity check -- this is an exact
  invertible transform for a logistic model (verified empirically: floating
  point-level agreement, no approximation introduced).
* XGBoost / sklearn tree ensembles -> shap.TreeExplainer with
  model_output="probability" and feature_perturbation="interventional",
  which explains SHAP values directly in probability space (they sum to
  predict_proba with no transform needed).

Both explainers are given the FULL batch as background data via an explicit
shap.maskers.Independent(..., max_samples=n_rows) masker -- without this,
shap silently subsamples any background >100 rows (still seeded/
deterministic, but an approximation we don't need at this dataset size).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import shap
from sklearn.ensemble import ExtraTreesClassifier, GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.tree import DecisionTreeClassifier
from xgboost import XGBClassifier

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ARTIFACTS_DIR = BASE_DIR / "models" / "artifacts"
METADATA_PATH = ARTIFACTS_DIR / "model_metadata.json"

TOP_K_FEATURES = 5
SANITY_CHECK_SAMPLE_SIZE = 10
SANITY_CHECK_TOLERANCE = 1e-4
RANDOM_SEED = 42  # used only to pick which 10 rows the sanity check samples

_TREE_MODEL_CLASSES = (
    XGBClassifier,
    RandomForestClassifier,
    GradientBoostingClassifier,
    ExtraTreesClassifier,
    DecisionTreeClassifier,
)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def load_metadata() -> dict:
    with open(METADATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_primary_pipeline(meta: dict):
    primary_key = meta["primary_model_key"]
    artifact_path = BASE_DIR / meta["models"][primary_key]["artifact_path"]
    pipeline = joblib.load(artifact_path)
    return pipeline, primary_key


def load_batch_df() -> pd.DataFrame:
    """The Phase 4 'batch' -- see module docstring for why this is
    train.csv + holdout.csv rather than a literal synthetic_batch.csv."""
    train_df = pd.read_csv(DATA_DIR / "train.csv")
    holdout_df = pd.read_csv(DATA_DIR / "holdout.csv")
    df = pd.concat([train_df, holdout_df], ignore_index=True)
    df["is_peak_execution_window"] = df["is_peak_execution_window"].astype(int)
    df = df.sort_values("case_id", kind="stable").reset_index(drop=True)
    return df


# --------------------------------------------------------------------------
# Feature-name mapping: transformed (one-hot / ordinal) columns -> the
# original raw column they came from, so top-5 features are reported against
# human-readable columns (e.g. "decline_code_bucket") rather than dummy
# columns (e.g. "cat__decline_code_bucket_AMBIGUOUS").
# --------------------------------------------------------------------------
def _map_transformed_to_original(
    transformed_names: list[str], numeric_cols: list[str], categorical_cols: list[str]
) -> list[str]:
    cat_by_length_desc = sorted(categorical_cols, key=len, reverse=True)
    mapping = []
    for name in transformed_names:
        rest = name.split("__", 1)[1] if "__" in name else name
        if rest in numeric_cols:
            mapping.append(rest)
            continue
        matched = next((c for c in cat_by_length_desc if rest == c or rest.startswith(c + "_")), None)
        if matched is None:
            raise ValueError(f"Could not map transformed SHAP feature '{name}' back to a raw column")
        mapping.append(matched)
    return mapping


def _to_jsonable(value: Any):
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


# --------------------------------------------------------------------------
# Explainer selection
# --------------------------------------------------------------------------
def _select_explainer(clf, background: np.ndarray):
    """Returns (explainer, is_probability_space)."""
    masker = shap.maskers.Independent(background, max_samples=background.shape[0])

    if isinstance(clf, LogisticRegression):
        explainer = shap.LinearExplainer(clf, masker)
        return explainer, False

    if isinstance(clf, _TREE_MODEL_CLASSES):
        if isinstance(clf, XGBClassifier):
            # This pipeline is always fit on a plain numeric ndarray (the
            # ColumnTransformer output), never a pandas categorical dtype,
            # so no split in the trained trees actually uses categorical
            # splitting -- but newer xgboost defaults enable_categorical=True
            # on the estimator regardless, which trips shap's categorical-
            # split guard. Clearing it reflects how the model was actually
            # trained and unblocks the exact "interventional" + "probability"
            # SHAP path.
            clf.enable_categorical = False
        explainer = shap.TreeExplainer(
            clf, data=masker, model_output="probability", feature_perturbation="interventional"
        )
        return explainer, True

    raise NotImplementedError(
        f"No SHAP explainer strategy configured for primary model type {type(clf)!r}. "
        "Expected LogisticRegression (LinearExplainer) or a tree-based classifier "
        "(TreeExplainer)."
    )


def _expected_value_scalar(explainer) -> float:
    base = explainer.expected_value
    if hasattr(base, "__len__"):
        base = np.asarray(base).reshape(-1)[0]
    return float(base)


# --------------------------------------------------------------------------
# Context: computed once, cached, reused by every public function below.
# --------------------------------------------------------------------------
class _ShapContext:
    def __init__(self):
        meta = load_metadata()
        pipeline, primary_key = load_primary_pipeline(meta)
        self.primary_key = primary_key

        self.df = load_batch_df()

        numeric_cols = meta["feature_columns"]["numeric"]
        categorical_cols = meta["feature_columns"]["categorical"]
        self.feature_columns = numeric_cols + categorical_cols

        preprocessor = pipeline.named_steps["preprocess"]
        clf = pipeline.named_steps["clf"]
        self.clf = clf

        X = self.df[self.feature_columns]
        self.Xt = preprocessor.transform(X)
        if hasattr(self.Xt, "toarray"):
            self.Xt = self.Xt.toarray()
        self.Xt = np.asarray(self.Xt, dtype=float)

        transformed_names = list(preprocessor.get_feature_names_out())
        self.column_mapping = _map_transformed_to_original(transformed_names, numeric_cols, categorical_cols)

        self.explainer, self.is_probability_space = _select_explainer(clf, self.Xt)
        self.base_value = _expected_value_scalar(self.explainer)

        shap_values = self.explainer.shap_values(self.Xt)
        self.shap_values = np.asarray(shap_values, dtype=float)

        self.predicted_proba = clf.predict_proba(self.Xt)[:, 1]

        self.case_id_to_row = {cid: i for i, cid in enumerate(self.df["case_id"])}

        run_additivity_sanity_check(self)

    def total_from_shap(self, row_idx: int) -> float:
        total = self.base_value + self.shap_values[row_idx].sum()
        if self.is_probability_space:
            return float(total)
        return float(1.0 / (1.0 + np.exp(-total)))


_context: _ShapContext | None = None


def _get_context() -> _ShapContext:
    global _context
    if _context is None:
        _context = _ShapContext()
    return _context


# --------------------------------------------------------------------------
# Sanity check (module docstring above explains the probability-space
# vs. logit-space handling)
# --------------------------------------------------------------------------
def run_additivity_sanity_check(
    ctx: "_ShapContext",
    n: int = SANITY_CHECK_SAMPLE_SIZE,
    seed: int = RANDOM_SEED,
    tolerance: float = SANITY_CHECK_TOLERANCE,
) -> None:
    n_rows = ctx.Xt.shape[0]
    sample_size = min(n, n_rows)
    rng = np.random.default_rng(seed)
    sample_idx = rng.choice(n_rows, size=sample_size, replace=False)

    failures = []
    for idx in sample_idx:
        reconstructed = ctx.total_from_shap(int(idx))
        actual = float(ctx.predicted_proba[idx])
        diff = abs(reconstructed - actual)
        if diff > tolerance:
            failures.append(
                f"case_id={ctx.df.iloc[idx]['case_id']} row={idx}: "
                f"base_value+sum(shap)={reconstructed:.8f} vs predict_proba={actual:.8f} "
                f"(diff={diff:.2e} > tol={tolerance:.0e})"
            )

    if failures:
        raise AssertionError(
            "SHAP additivity sanity check failed for "
            f"{len(failures)}/{sample_size} sampled rows:\n" + "\n".join(failures)
        )


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def _top_features_for_row(ctx: "_ShapContext", row_idx: int) -> list[dict]:
    raw_row = ctx.df.iloc[row_idx]
    agg: dict[str, float] = {}
    for transformed_idx, original_col in enumerate(ctx.column_mapping):
        agg[original_col] = agg.get(original_col, 0.0) + float(ctx.shap_values[row_idx, transformed_idx])

    ranked = sorted(agg.items(), key=lambda kv: abs(kv[1]), reverse=True)[:TOP_K_FEATURES]
    return [
        {"feature": feature, "value": _to_jsonable(raw_row[feature]), "shap_value": float(shap_val)}
        for feature, shap_val in ranked
    ]


def get_shap_top_features(row_id: str) -> list[dict]:
    """row_id is the case_id (e.g. "case_0042")."""
    ctx = _get_context()
    if row_id not in ctx.case_id_to_row:
        raise KeyError(f"Unknown case_id: {row_id!r}")
    return _top_features_for_row(ctx, ctx.case_id_to_row[row_id])


def get_case_facts(row_id: str) -> dict:
    """Raw case facts for row_id, for the Phase 5 LLM prompt -- the model's
    feature columns plus case_id/decline_code/guardrail_flags. Deliberately
    excludes 'outcome' (the historical target label) so the prompt never
    leaks the answer for what was, at scoring time, an unresolved case."""
    ctx = _get_context()
    if row_id not in ctx.case_id_to_row:
        raise KeyError(f"Unknown case_id: {row_id!r}")
    row = ctx.df.iloc[ctx.case_id_to_row[row_id]]
    fact_cols = ["case_id", "decline_code", "guardrail_flags"] + ctx.feature_columns
    facts = {}
    for col in fact_cols:
        value = row[col]
        facts[col] = None if pd.isna(value) else _to_jsonable(value)
    # Phase 6's network_retry_cap_exceeded guardrail wants a trailing-30-day
    # cross-transaction retry count, but case_id is a single transaction with
    # no customer identity or timestamp linking it to other transactions in
    # this data model -- a true 30-day rolling count isn't derivable. This is
    # an honestly-named same-transaction proxy (this case's own retry count
    # so far) rather than a mislabeled 30-day figure.
    facts["cumulative_retries_this_txn"] = facts["retry_attempt_number"]
    return facts


def get_tree_model_score(row_id: str) -> float:
    ctx = _get_context()
    if row_id not in ctx.case_id_to_row:
        raise KeyError(f"Unknown case_id: {row_id!r}")
    return float(ctx.predicted_proba[ctx.case_id_to_row[row_id]])


_live_pipeline_cache: tuple | None = None


def _get_live_pipeline():
    """Lazily-cached (preprocessor, clf) pair, loaded once from the same
    artifact as _ShapContext. Kept separate from _ShapContext (rather than
    stashing these on it) so score_new_case below never has to touch that
    class's internals."""
    global _live_pipeline_cache
    if _live_pipeline_cache is None:
        meta = load_metadata()
        pipeline, _primary_key = load_primary_pipeline(meta)
        _live_pipeline_cache = (pipeline.named_steps["preprocess"], pipeline.named_steps["clf"])
    return _live_pipeline_cache


def score_new_case(case_facts: dict) -> dict:
    """Score a case that is NOT part of the loaded train/holdout batch --
    e.g. a live Razorpay webhook case -- against the same primary-model
    pipeline and SHAP explainer used for the batch above.

    Added for webhook_receiver.py: every other function in this module keys
    off a case_id already present in data/train.csv + data/holdout.csv
    (get_shap_top_features / get_case_facts / get_tree_model_score), which a
    real-time webhook case never is. This reuses the same cached explainer
    and background data (_get_context()) rather than re-fitting per request,
    so live scoring stays consistent with the batch's own SHAP baseline.

    case_facts must contain every key in the model's feature_columns
    (numeric + categorical, from model_metadata.json) -- see
    webhook_receiver.map_payload_to_case for how a Razorpay webhook payload
    is mapped into that shape. Missing keys transform as NaN/None, same as
    an unmapped category would in the training pipeline.

    Returns {"tree_model_score": float, "shap_top_features": [...]}, the
    same shapes get_tree_model_score / get_shap_top_features return.
    """
    ctx = _get_context()
    preprocessor, clf = _get_live_pipeline()

    row_df = pd.DataFrame([{col: case_facts.get(col) for col in ctx.feature_columns}])
    Xt_row = preprocessor.transform(row_df)
    if hasattr(Xt_row, "toarray"):
        Xt_row = Xt_row.toarray()
    Xt_row = np.asarray(Xt_row, dtype=float)

    tree_model_score = float(clf.predict_proba(Xt_row)[:, 1][0])

    shap_row = np.asarray(ctx.explainer.shap_values(Xt_row), dtype=float)
    if shap_row.ndim == 2:
        shap_row = shap_row[0]

    agg: dict[str, float] = {}
    for transformed_idx, original_col in enumerate(ctx.column_mapping):
        agg[original_col] = agg.get(original_col, 0.0) + float(shap_row[transformed_idx])
    ranked = sorted(agg.items(), key=lambda kv: abs(kv[1]), reverse=True)[:TOP_K_FEATURES]
    shap_top_features = [
        {"feature": feature, "value": _to_jsonable(case_facts.get(feature)), "shap_value": shap_val}
        for feature, shap_val in ranked
    ]

    return {"tree_model_score": tree_model_score, "shap_top_features": shap_top_features}


def get_scores_df() -> pd.DataFrame:
    """case_id, tree_model_score, decline_code (raw, un-bucketed) for every
    case -- the tree_model_score is exactly what SHAP explained above, so
    Phase 4's routing and SHAP outputs are guaranteed to agree."""
    ctx = _get_context()
    return pd.DataFrame(
        {
            "case_id": ctx.df["case_id"].to_numpy(),
            "tree_model_score": ctx.predicted_proba,
            "decline_code": ctx.df["decline_code"].to_numpy(),
        }
    )


def run_full_batch() -> dict:
    ctx = _get_context()
    return {case_id: _top_features_for_row(ctx, row_idx) for case_id, row_idx in ctx.case_id_to_row.items()}


if __name__ == "__main__":
    result = run_full_batch()
    print(f"Computed SHAP top-{TOP_K_FEATURES} features for {len(result)} cases.")
    sample_case = next(iter(result))
    print(f"Example ({sample_case}): {result[sample_case]}")
