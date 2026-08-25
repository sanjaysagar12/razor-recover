"""
Phase 3 -- Tree Model Layer.

Trains a logistic-regression baseline and a heavily regularized XGBoost
classifier to predict whether a failed subscription payment will recover,
compares them with proper scoring rules (log loss / Brier score), and
saves whichever one wins as PRIMARY_MODEL for Phase 4 (SHAP) and
Phase 7 (audit logging) to consume.

--------------------------------------------------------------------------
Data-source note (read this before touching column names below)
--------------------------------------------------------------------------
The Phase 3 spec assumes a single `data/synthetic_batch.csv` with a
`recovered` target and columns like `issuer_bank`. The actual Phase 2
output in this repo is `data/train.csv` + `data/holdout.csv`, target
column `outcome`, and a few renamed/extra columns. This script adapts to
what Phase 2 actually produced:

  * We concatenate train.csv + holdout.csv back into one pool and cut our
    own reproducible 70/15/15 (train/val/test) split from it, rather than
    reusing Phase 2's 80/20 generation-time split -- Phase 3 needs its own
    validation slice for early stopping / hyperparameter selection, which
    Phase 2's split doesn't carve out.
  * `outcome` is the target (1 = recovered).
  * `decline_code_bucket` (3 categories: CLEAR_HARD / CLEAR_SOFT /
    AMBIGUOUS) is used as the categorical decline-reason feature instead
    of the finer-grained `decline_code` (10 categories). Per
    data/generation_notes.md, the bucket is what actually drives the
    label-generating rules, and with ~280 rows total, 10 categories would
    leave several with single-digit support -- too sparse for a stable
    one-hot / ordinal encoding.
  * `guardrail_flags` and `decline_code` are dropped as model inputs.
    `guardrail_flags` is a deterministic function of columns we already
    include (decline_code_bucket, payment_rail, retry_attempt_number,
    is_peak_execution_window, amount_vs_historical_avg -- see
    generation_notes.md), so keeping it would just hand both models a
    redundant, perfectly-collinear shortcut feature instead of making
    them learn the underlying interactions. `case_id` is an identifier,
    not a feature.
--------------------------------------------------------------------------
"""

from __future__ import annotations

import itertools
import json
from datetime import date
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    brier_score_loss,
    f1_score,
    log_loss,
    precision_score,
    recall_score,
)
from sklearn.model_selection import StratifiedKFold, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder, StandardScaler
from xgboost import XGBClassifier

# --------------------------------------------------------------------------
# Paths & global config
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
MODELS_DIR = BASE_DIR / "models"
ARTIFACTS_DIR = MODELS_DIR / "artifacts"
REPORT_PATH = MODELS_DIR / "model_report.md"

RANDOM_SEED = 42
RUN_DATE = date.today().isoformat()  # used in model version strings

TARGET_COL = "outcome"
DROP_COLUMNS = ["case_id", "decline_code", "guardrail_flags"]

NUMERIC_FEATURES = [
    "retry_attempt_number",
    "hours_since_last_attempt",
    "customer_tenure_days",
    "historical_ptp_honor_rate",
    "amount",
    "amount_vs_historical_avg",
    "prior_retry_success_count",
    "is_peak_execution_window",  # boolean, cast to 0/1 below -- numeric for both models
]
CATEGORICAL_FEATURES = [
    "decline_code_bucket",
    "ltv_tier",
    "time_of_day_bucket",
    "day_of_week",
    "issuer_bank_risk_tier",
    "payment_rail",
]
FEATURE_COLUMNS = NUMERIC_FEATURES + CATEGORICAL_FEATURES

# XGBoost hyperparameter grid. Every value here is deliberately conservative
# -- see the comment above the grid-search loop for why.
XGB_GRID = {
    "max_depth": [2, 3],
    "learning_rate": [0.01, 0.05],
    "subsample": [0.6, 0.8],
    "colsample_bytree": [0.6, 0.8],
    "min_child_weight": [3, 5],
    "reg_lambda": [1.5, 3.0],
}
LOGREG_C_GRID = [0.01, 0.1, 1, 10]


# --------------------------------------------------------------------------
# Sanity-check helper (step 7) -- raises loudly, never swallows a failure.
# --------------------------------------------------------------------------
def sanity_check_probabilities(proba: np.ndarray, model_name: str) -> None:
    proba = np.asarray(proba, dtype=float)
    if np.isnan(proba).any():
        raise ValueError(f"[{model_name}] predicted probabilities contain NaN values")
    if not np.all((proba >= 0.0) & (proba <= 1.0)):
        bad = proba[(proba < 0.0) | (proba > 1.0)]
        raise ValueError(
            f"[{model_name}] predicted probabilities outside [0,1]: examples {bad[:5]}"
        )
    if np.unique(np.round(proba, 8)).size <= 1:
        raise ValueError(
            f"[{model_name}] predicted the same probability for every test row "
            f"({proba[0]:.6f}) -- model is not discriminating at all"
        )


# --------------------------------------------------------------------------
# Data loading & splitting (step 1)
# --------------------------------------------------------------------------
def load_data() -> pd.DataFrame:
    train_raw = pd.read_csv(DATA_DIR / "train.csv")
    holdout_raw = pd.read_csv(DATA_DIR / "holdout.csv")
    df = pd.concat([train_raw, holdout_raw], ignore_index=True)
    df["is_peak_execution_window"] = df["is_peak_execution_window"].astype(int)
    return df


def make_split(df: pd.DataFrame):
    """Stratified 70/15/15 train/val/test split on the target, seed=42."""
    train_val_df, test_df = train_test_split(
        df,
        test_size=0.15,
        stratify=df[TARGET_COL],
        random_state=RANDOM_SEED,
    )
    # 0.15 / 0.85 of the remainder == 15% of the original whole.
    train_df, val_df = train_test_split(
        train_val_df,
        test_size=0.15 / 0.85,
        stratify=train_val_df[TARGET_COL],
        random_state=RANDOM_SEED,
    )
    return train_df, val_df, test_df, train_val_df


def save_split_indices(train_df, val_df, test_df) -> None:
    """Persist the case_id membership of each split so it is auditable and
    reproducible independent of re-running train_test_split."""
    payload = {
        "random_seed": RANDOM_SEED,
        "split_sizes": {"train": len(train_df), "val": len(val_df), "test": len(test_df)},
        "case_ids": {
            "train": sorted(train_df["case_id"].tolist()),
            "val": sorted(val_df["case_id"].tolist()),
            "test": sorted(test_df["case_id"].tolist()),
        },
    }
    with open(ARTIFACTS_DIR / "data_split.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


# --------------------------------------------------------------------------
# Preprocessing pipelines (step 2) -- same source columns, different encodings.
# --------------------------------------------------------------------------
def build_logreg_preprocessor() -> ColumnTransformer:
    return ColumnTransformer(
        transformers=[
            ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), CATEGORICAL_FEATURES),
            ("num", StandardScaler(), NUMERIC_FEATURES),
        ]
    )


def build_xgb_preprocessor() -> ColumnTransformer:
    return ColumnTransformer(
        transformers=[
            (
                "cat",
                OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1),
                CATEGORICAL_FEATURES,
            ),
            ("num", "passthrough", NUMERIC_FEATURES),
        ]
    )


# --------------------------------------------------------------------------
# Step 3: logistic regression baseline, C tuned on the validation set.
# --------------------------------------------------------------------------
def tune_logreg(train_df, val_df):
    prep = build_logreg_preprocessor()
    X_train = prep.fit_transform(train_df[FEATURE_COLUMNS])
    X_val = prep.transform(val_df[FEATURE_COLUMNS])
    y_train, y_val = train_df[TARGET_COL].to_numpy(), val_df[TARGET_COL].to_numpy()

    results = []
    best_c, best_logloss = None, np.inf
    for c in LOGREG_C_GRID:
        # penalty="l2" is the implicit default here (l1_ratio=0.0); passing
        # penalty= explicitly is deprecated in this sklearn version.
        clf = LogisticRegression(C=c, solver="lbfgs", max_iter=2000, random_state=RANDOM_SEED)
        clf.fit(X_train, y_train)
        val_proba = clf.predict_proba(X_val)[:, 1]
        ll = log_loss(y_val, val_proba, labels=[0, 1])
        results.append({"C": c, "val_log_loss": ll})
        if ll < best_logloss:
            best_logloss, best_c = ll, c

    print(f"  LogReg C grid search (selecting by validation log loss): {results}")
    print(f"  -> chosen C = {best_c} (val log loss = {best_logloss:.4f})")
    return best_c, results


# --------------------------------------------------------------------------
# Step 4: heavily regularized XGBoost.
#
# WHY these ranges and not XGBoost's defaults: the dataset is ~280 rows.
# Default XGBoost (max_depth=6, no subsampling, min_child_weight=1,
# reg_lambda=1, unlimited rounds) will happily carve out a leaf for every
# handful of training rows and memorize noise rather than the underlying
# recovery-probability rules. Every knob below exists specifically to stop
# that:
#   - max_depth in {2,3}: shallow trees, few splits per tree.
#   - learning_rate in {0.01,0.05}: small steps so no single tree/round can
#     dominate the ensemble.
#   - subsample/colsample_bytree in {0.6,0.8}: each round only sees a random
#     slice of rows/columns, so a single outlier row or column can't be
#     memorized every round.
#   - min_child_weight >= 3: a leaf must cover at least ~3 rows worth of
#     Hessian weight, which rules out single-row leaves.
#   - reg_lambda >= 1.5: extra L2 shrinkage on leaf weights, above
#     XGBoost's default of 1.0.
#   - n_estimators=500 + early_stopping_rounds=20 on validation log loss:
#     lets boosting run long enough to be useful, but stops as soon as
#     validation performance stops improving for 20 rounds, instead of
#     fitting all 500 trees regardless of overfitting.
# This is an anti-overfitting constraint given the small dataset, not a
# performance tweak -- on more data these would likely hurt accuracy.
# --------------------------------------------------------------------------
def tune_xgb(train_df, val_df):
    prep = build_xgb_preprocessor()
    X_train = prep.fit_transform(train_df[FEATURE_COLUMNS])
    X_val = prep.transform(val_df[FEATURE_COLUMNS])
    y_train, y_val = train_df[TARGET_COL].to_numpy(), val_df[TARGET_COL].to_numpy()

    keys = list(XGB_GRID.keys())
    combos = list(itertools.product(*[XGB_GRID[k] for k in keys]))

    best_params, best_logloss, best_n_estimators = None, np.inf, None
    for combo in combos:
        params = dict(zip(keys, combo))
        clf = XGBClassifier(
            n_estimators=500,
            early_stopping_rounds=20,
            eval_metric="logloss",
            objective="binary:logistic",
            random_state=RANDOM_SEED,
            **params,
        )
        clf.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)
        val_logloss = clf.best_score
        if val_logloss < best_logloss:
            best_logloss = val_logloss
            best_params = params
            # best_iteration is 0-indexed; +1 gives the tree count to lock
            # in when we later refit without early stopping.
            best_n_estimators = clf.best_iteration + 1

    print(f"  XGBoost grid search: {len(combos)} combinations, selecting by validation log loss")
    print(f"  -> chosen params = {best_params}")
    print(f"  -> chosen n_estimators (from early stopping) = {best_n_estimators} (val log loss = {best_logloss:.4f})")
    return best_params, best_n_estimators, best_logloss


# --------------------------------------------------------------------------
# Step 5: 5-fold stratified CV on train+val, for both models.
#
# XGBoost's boosting-round count is fixed at the early-stopping optimum
# found above rather than re-run per fold: fold sizes here (~48 rows) are
# too small to carve out yet another internal validation split for
# per-fold early stopping without the folds becoming unstable themselves.
# --------------------------------------------------------------------------
def cross_validate(train_val_df, logreg_c, xgb_params, xgb_n_estimators):
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=RANDOM_SEED)
    y_all = train_val_df[TARGET_COL].to_numpy()

    fold_metrics = {
        "logreg": {"f1": [], "brier": []},
        "xgboost": {"f1": [], "brier": []},
    }

    for fold_idx, (tr_idx, va_idx) in enumerate(skf.split(train_val_df[FEATURE_COLUMNS], y_all), start=1):
        fold_train = train_val_df.iloc[tr_idx]
        fold_val = train_val_df.iloc[va_idx]
        y_tr = fold_train[TARGET_COL].to_numpy()
        y_va = fold_val[TARGET_COL].to_numpy()

        # LogReg fold fit
        lr_prep = clone(build_logreg_preprocessor())
        Xtr = lr_prep.fit_transform(fold_train[FEATURE_COLUMNS])
        Xva = lr_prep.transform(fold_val[FEATURE_COLUMNS])
        lr_clf = LogisticRegression(C=logreg_c, solver="lbfgs", max_iter=2000, random_state=RANDOM_SEED)
        lr_clf.fit(Xtr, y_tr)
        lr_proba = lr_clf.predict_proba(Xva)[:, 1]
        fold_metrics["logreg"]["f1"].append(f1_score(y_va, (lr_proba >= 0.5).astype(int)))
        fold_metrics["logreg"]["brier"].append(brier_score_loss(y_va, lr_proba))

        # XGBoost fold fit (fixed n_estimators, no early stopping inside CV)
        xgb_prep = clone(build_xgb_preprocessor())
        Xtr_x = xgb_prep.fit_transform(fold_train[FEATURE_COLUMNS])
        Xva_x = xgb_prep.transform(fold_val[FEATURE_COLUMNS])
        xgb_clf = XGBClassifier(
            n_estimators=xgb_n_estimators,
            eval_metric="logloss",
            objective="binary:logistic",
            random_state=RANDOM_SEED,
            **xgb_params,
        )
        xgb_clf.fit(Xtr_x, y_tr)
        xgb_proba = xgb_clf.predict_proba(Xva_x)[:, 1]
        fold_metrics["xgboost"]["f1"].append(f1_score(y_va, (xgb_proba >= 0.5).astype(int)))
        fold_metrics["xgboost"]["brier"].append(brier_score_loss(y_va, xgb_proba))

        print(
            f"  Fold {fold_idx}: "
            f"LogReg F1={fold_metrics['logreg']['f1'][-1]:.3f} Brier={fold_metrics['logreg']['brier'][-1]:.3f} | "
            f"XGB F1={fold_metrics['xgboost']['f1'][-1]:.3f} Brier={fold_metrics['xgboost']['brier'][-1]:.3f}"
        )

    cv_summary = {}
    for model_key in ("logreg", "xgboost"):
        cv_summary[model_key] = {
            "f1_mean": float(np.mean(fold_metrics[model_key]["f1"])),
            "f1_std": float(np.std(fold_metrics[model_key]["f1"])),
            "brier_mean": float(np.mean(fold_metrics[model_key]["brier"])),
            "brier_std": float(np.std(fold_metrics[model_key]["brier"])),
        }

    print(
        "  CV summary: "
        f"LogReg F1={cv_summary['logreg']['f1_mean']:.3f}+/-{cv_summary['logreg']['f1_std']:.3f}, "
        f"Brier={cv_summary['logreg']['brier_mean']:.3f}+/-{cv_summary['logreg']['brier_std']:.3f} | "
        f"XGB F1={cv_summary['xgboost']['f1_mean']:.3f}+/-{cv_summary['xgboost']['f1_std']:.3f}, "
        f"Brier={cv_summary['xgboost']['brier_mean']:.3f}+/-{cv_summary['xgboost']['brier_std']:.3f}"
    )

    # Overfitting-instability flag: XGBoost's fold-to-fold variance should
    # not dwarf logistic regression's. If it's more than 2x, the tree model
    # is not learning something stable given how little data we have.
    f1_ratio = cv_summary["xgboost"]["f1_std"] / max(cv_summary["logreg"]["f1_std"], 1e-9)
    brier_ratio = cv_summary["xgboost"]["brier_std"] / max(cv_summary["logreg"]["brier_std"], 1e-9)
    overfitting_flag = {
        "f1_std_ratio": f1_ratio,
        "brier_std_ratio": brier_ratio,
        "f1_std_exceeds_2x": bool(f1_ratio > 2.0),
        "brier_std_exceeds_2x": bool(brier_ratio > 2.0),
    }
    if overfitting_flag["f1_std_exceeds_2x"] or overfitting_flag["brier_std_exceeds_2x"]:
        print(
            "  *** WARNING: XGBoost's CV std dev is more than 2x logistic regression's "
            f"(F1 ratio={f1_ratio:.2f}x, Brier ratio={brier_ratio:.2f}x) -- this indicates "
            "overfitting instability, not a genuinely stronger model. ***"
        )
    else:
        print(
            f"  XGBoost CV variance is within 2x of logistic regression's "
            f"(F1 ratio={f1_ratio:.2f}x, Brier ratio={brier_ratio:.2f}x) -- no overfitting-instability flag."
        )

    return cv_summary, overfitting_flag


# --------------------------------------------------------------------------
# Step 6: single held-out test evaluation, naive baseline, calibration bins.
# --------------------------------------------------------------------------
def fit_final_pipelines(train_val_df, logreg_c, xgb_params, xgb_n_estimators):
    """Refit both models on the full train+val portion (never touching test)."""
    X = train_val_df[FEATURE_COLUMNS]
    y = train_val_df[TARGET_COL].to_numpy()

    logreg_pipeline = Pipeline(
        [
            ("preprocess", build_logreg_preprocessor()),
            ("clf", LogisticRegression(C=logreg_c, solver="lbfgs", max_iter=2000, random_state=RANDOM_SEED)),
        ]
    )
    logreg_pipeline.fit(X, y)

    xgb_pipeline = Pipeline(
        [
            ("preprocess", build_xgb_preprocessor()),
            (
                "clf",
                XGBClassifier(
                    n_estimators=xgb_n_estimators,
                    eval_metric="logloss",
                    objective="binary:logistic",
                    random_state=RANDOM_SEED,
                    **xgb_params,
                ),
            ),
        ]
    )
    xgb_pipeline.fit(X, y)

    return logreg_pipeline, xgb_pipeline


def evaluate_on_test(pipeline, test_df, model_name):
    X_test = test_df[FEATURE_COLUMNS]
    y_test = test_df[TARGET_COL].to_numpy()

    proba = pipeline.predict_proba(X_test)[:, 1]
    sanity_check_probabilities(proba, model_name)

    preds = (proba >= 0.5).astype(int)
    if np.unique(preds).size <= 1:
        raise ValueError(f"[{model_name}] predicted a single class for the entire test set")

    metrics = {
        "precision": float(precision_score(y_test, preds, zero_division=0)),
        "recall": float(recall_score(y_test, preds, zero_division=0)),
        "f1": float(f1_score(y_test, preds, zero_division=0)),
        "brier": float(brier_score_loss(y_test, proba)),
    }
    return metrics, proba, y_test


def calibration_table(proba, y_test, n_bins=4) -> pd.DataFrame:
    """Bucket predictions into n_bins quantile bins; compare mean predicted
    probability to observed outcome rate per bin. Quantile bins (rather
    than fixed-width 0-0.25/0.25-0.5/...) are used because the test set is
    only ~40 rows -- fixed-width bins would leave some nearly empty."""
    df = pd.DataFrame({"proba": proba, "outcome": y_test})
    df["bin"] = pd.qcut(df["proba"], q=n_bins, duplicates="drop")
    grouped = df.groupby("bin", observed=True).agg(
        n=("outcome", "size"),
        mean_predicted_prob=("proba", "mean"),
        observed_outcome_rate=("outcome", "mean"),
    )
    return grouped.reset_index()


# --------------------------------------------------------------------------
# Report generation (step 8)
# --------------------------------------------------------------------------
def build_report_markdown(
    cv_summary,
    overfitting_flag,
    logreg_metrics,
    xgb_metrics,
    naive_brier,
    primary_key,
    primary_version,
    calib_df,
    logreg_c,
    xgb_params,
    xgb_n_estimators,
    split_sizes,
) -> str:
    def fmt(x):
        return f"{x:.4f}"

    calib_lines = "\n".join(
        f"| {row.bin} | {row.n} | {fmt(row.mean_predicted_prob)} | {fmt(row.observed_outcome_rate)} |"
        for row in calib_df.itertuples()
    )

    flag_line = (
        "**FLAGGED:** XGBoost's cross-validation standard deviation is more than 2x "
        "logistic regression's, indicating overfitting instability rather than a "
        "genuinely stronger model."
        if (overfitting_flag["f1_std_exceeds_2x"] or overfitting_flag["brier_std_exceeds_2x"])
        else "Not flagged: XGBoost's cross-validation standard deviation stays within 2x of logistic regression's."
    )

    if primary_key == "xgboost":
        verdict = (
            f"**PRIMARY_MODEL = {primary_version} (XGBoost).** On the held-out test set, XGBoost's "
            f"Brier score ({fmt(xgb_metrics['brier'])}) is lower than logistic regression's "
            f"({fmt(logreg_metrics['brier'])}) -- Brier score is a proper scoring rule that rewards "
            "well-calibrated probabilities, not just correct hard classifications, so it is the "
            "deciding metric rather than F1 or accuracy. XGBoost is selected as PRIMARY_MODEL on that "
            "basis."
        )
    else:
        verdict = (
            f"**PRIMARY_MODEL = {primary_version} (Logistic Regression).** On the held-out test set, "
            f"logistic regression's Brier score ({fmt(logreg_metrics['brier'])}) is equal to or better "
            f"than XGBoost's ({fmt(xgb_metrics['brier'])}). XGBoost does NOT clearly beat logistic "
            "regression on this dataset -- with only ~280 rows, the extra flexibility of a tree "
            "ensemble does not translate into better-calibrated probabilities than a simple, heavily "
            "regularized linear model. Rather than pick XGBoost by default, logistic regression is "
            "selected as PRIMARY_MODEL because it wins (or ties) on the proper scoring rule."
        )

    return f"""# Phase 3 Model Report

Run date: {RUN_DATE} | Random seed: {RANDOM_SEED}

Split sizes: train={split_sizes['train']}, val={split_sizes['val']}, test={split_sizes['test']}

## LogReg vs XGBoost

| Metric | Logistic Regression | XGBoost |
|---|---|---|
| Chosen hyperparameters | C={logreg_c} | {xgb_params}, n_estimators={xgb_n_estimators} |
| CV F1 (mean +/- std) | {fmt(cv_summary['logreg']['f1_mean'])} +/- {fmt(cv_summary['logreg']['f1_std'])} | {fmt(cv_summary['xgboost']['f1_mean'])} +/- {fmt(cv_summary['xgboost']['f1_std'])} |
| CV Brier (mean +/- std) | {fmt(cv_summary['logreg']['brier_mean'])} +/- {fmt(cv_summary['logreg']['brier_std'])} | {fmt(cv_summary['xgboost']['brier_mean'])} +/- {fmt(cv_summary['xgboost']['brier_std'])} |
| Test Precision | {fmt(logreg_metrics['precision'])} | {fmt(xgb_metrics['precision'])} |
| Test Recall | {fmt(logreg_metrics['recall'])} | {fmt(xgb_metrics['recall'])} |
| Test F1 | {fmt(logreg_metrics['f1'])} | {fmt(xgb_metrics['f1'])} |
| Test Brier score | {fmt(logreg_metrics['brier'])} | {fmt(xgb_metrics['brier'])} |

### Overfitting-instability check

CV std ratio (XGBoost / LogReg): F1={overfitting_flag['f1_std_ratio']:.2f}x, Brier={overfitting_flag['brier_std_ratio']:.2f}x

{flag_line}

## Naive baseline

A constant prediction equal to the training set's class prior gives a Brier score of
**{fmt(naive_brier)}** on the test set. Both trained models should beat this to be worth deploying.

## Calibration check ({primary_version}, primary model)

Predictions bucketed into quantile bins by predicted probability:

| Probability bin | n | Mean predicted probability | Observed outcome rate |
|---|---|---|---|
{calib_lines}

## Verdict

{verdict}
"""


# --------------------------------------------------------------------------
# Artifact saving (step 9)
# --------------------------------------------------------------------------
def save_artifacts(
    logreg_pipeline,
    xgb_pipeline,
    logreg_c,
    xgb_params,
    xgb_n_estimators,
    cv_summary,
    logreg_metrics,
    xgb_metrics,
    naive_brier,
    overfitting_flag,
    primary_key,
    split_sizes,
):
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    logreg_version = f"logreg_v1_{RUN_DATE}"
    xgb_version = f"xgb_v1_{RUN_DATE}"
    primary_version = xgb_version if primary_key == "xgboost" else logreg_version

    joblib.dump(logreg_pipeline, ARTIFACTS_DIR / "logreg_pipeline.joblib")
    joblib.dump(xgb_pipeline, ARTIFACTS_DIR / "xgb_pipeline.joblib")

    metadata = {
        "random_seed": RANDOM_SEED,
        "generated_at": RUN_DATE,
        "data_source": ["data/train.csv", "data/holdout.csv"],
        "target_column": TARGET_COL,
        "feature_columns": {
            "numeric": NUMERIC_FEATURES,
            "categorical": CATEGORICAL_FEATURES,
        },
        "dropped_columns": DROP_COLUMNS,
        "split_sizes": split_sizes,
        "models": {
            "logreg": {
                "version": logreg_version,
                "artifact_path": "models/artifacts/logreg_pipeline.joblib",
                "hyperparameters": {"penalty": "l2", "solver": "lbfgs", "C": logreg_c},
                "cv_f1_mean": cv_summary["logreg"]["f1_mean"],
                "cv_f1_std": cv_summary["logreg"]["f1_std"],
                "cv_brier_mean": cv_summary["logreg"]["brier_mean"],
                "cv_brier_std": cv_summary["logreg"]["brier_std"],
                "test_metrics": logreg_metrics,
            },
            "xgboost": {
                "version": xgb_version,
                "artifact_path": "models/artifacts/xgb_pipeline.joblib",
                "hyperparameters": {
                    **xgb_params,
                    "n_estimators": xgb_n_estimators,
                    "objective": "binary:logistic",
                    "eval_metric": "logloss",
                },
                "cv_f1_mean": cv_summary["xgboost"]["f1_mean"],
                "cv_f1_std": cv_summary["xgboost"]["f1_std"],
                "cv_brier_mean": cv_summary["xgboost"]["brier_mean"],
                "cv_brier_std": cv_summary["xgboost"]["brier_std"],
                "test_metrics": xgb_metrics,
            },
        },
        "naive_baseline_test_brier": naive_brier,
        "overfitting_flag": overfitting_flag,
        "primary_model": primary_version,
        "primary_model_key": primary_key,
    }
    with open(ARTIFACTS_DIR / "model_metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    return logreg_version, xgb_version, primary_version


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    np.random.seed(RANDOM_SEED)

    print("=" * 70)
    print("Phase 3: Tree Model Layer")
    print("=" * 70)

    print("\n[1/6] Loading data and building 70/15/15 stratified split ...")
    df = load_data()
    train_df, val_df, test_df, train_val_df = make_split(df)
    split_sizes = {"train": len(train_df), "val": len(val_df), "test": len(test_df)}
    save_split_indices(train_df, val_df, test_df)
    print(f"  Loaded {len(df)} rows. Split -> {split_sizes}")
    print(f"  Class balance (outcome=1 rate): overall={df[TARGET_COL].mean():.3f}, "
          f"train={train_df[TARGET_COL].mean():.3f}, val={val_df[TARGET_COL].mean():.3f}, "
          f"test={test_df[TARGET_COL].mean():.3f}")

    print("\n[2/6] Tuning logistic regression (C) on validation set ...")
    logreg_c, _ = tune_logreg(train_df, val_df)

    print("\n[3/6] Tuning regularized XGBoost on validation set (early stopping on log loss) ...")
    xgb_params, xgb_n_estimators, _ = tune_xgb(train_df, val_df)

    print("\n[4/6] Running 5-fold stratified CV on train+val for both models ...")
    cv_summary, overfitting_flag = cross_validate(train_val_df, logreg_c, xgb_params, xgb_n_estimators)

    print("\n[5/6] Refitting on train+val and evaluating ONCE on held-out test ...")
    logreg_pipeline, xgb_pipeline = fit_final_pipelines(train_val_df, logreg_c, xgb_params, xgb_n_estimators)

    logreg_metrics, logreg_proba, y_test = evaluate_on_test(logreg_pipeline, test_df, "logreg")
    xgb_metrics, xgb_proba, _ = evaluate_on_test(xgb_pipeline, test_df, "xgboost")

    train_prior = train_df[TARGET_COL].mean()
    naive_brier = float(np.mean((train_prior - y_test) ** 2))

    print(f"  LogReg test: {logreg_metrics}")
    print(f"  XGBoost test: {xgb_metrics}")
    print(f"  Naive baseline (constant={train_prior:.3f}) test Brier: {naive_brier:.4f}")

    # Model selection: Brier score (proper scoring rule) decides PRIMARY_MODEL.
    # Ties go to logistic regression -- prefer the simpler model unless
    # XGBoost clearly wins.
    if xgb_metrics["brier"] < logreg_metrics["brier"]:
        primary_key = "xgboost"
        primary_proba = xgb_proba
    else:
        primary_key = "logreg"
        primary_proba = logreg_proba
    print(f"  -> PRIMARY_MODEL selected by test Brier score: {primary_key}")

    calib_df = calibration_table(primary_proba, y_test, n_bins=4)
    print("\n  Calibration table (primary model):")
    print(calib_df.to_string(index=False))

    print("\n[6/6] Saving artifacts and report ...")
    logreg_version, xgb_version, primary_version = save_artifacts(
        logreg_pipeline,
        xgb_pipeline,
        logreg_c,
        xgb_params,
        xgb_n_estimators,
        cv_summary,
        logreg_metrics,
        xgb_metrics,
        naive_brier,
        overfitting_flag,
        primary_key,
        split_sizes,
    )

    report_md = build_report_markdown(
        cv_summary,
        overfitting_flag,
        logreg_metrics,
        xgb_metrics,
        naive_brier,
        primary_key,
        primary_version,
        calib_df,
        logreg_c,
        xgb_params,
        xgb_n_estimators,
        split_sizes,
    )
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report_md)

    print(f"  Wrote {REPORT_PATH}")
    print(f"  Wrote artifacts to {ARTIFACTS_DIR} (logreg_pipeline.joblib, xgb_pipeline.joblib, "
          f"model_metadata.json, data_split.json)")
    print(f"\nDone. PRIMARY_MODEL = {primary_version}")


if __name__ == "__main__":
    main()
