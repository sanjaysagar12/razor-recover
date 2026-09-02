"""
Dashboard "Model" page support.

Two training paths, both reusing train_tree_models.py's own functions
rather than re-implementing the LogReg-vs-XGBoost comparison:

  * Retrain (official) -- delegates straight to train_tree_models.main(),
    completely unchanged. Still writes to models/artifacts/*.joblib +
    models/model_report.md, i.e. it replaces the model the live pipeline
    will load on its next restart (shap_extract.py caches the pipeline
    in-process, so an already-running webhook_receiver.py keeps scoring
    with whatever it already loaded -- this only takes effect after a
    restart). Callers should treat this as a real, consequential action.

  * Custom dataset -- an uploaded CSV, validated against the exact same
    feature/target schema train_tree_models.py trains on, run through the
    same tune -> cross-validate -> fit -> evaluate -> report pipeline via
    its individual functions, and written to models/custom_runs/<id>/
    instead. Deliberately never touches data/train.csv, data/holdout.csv,
    models/artifacts/, or models/model_report.md -- uploads are additive,
    stored separately under data/uploads/.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import train_tree_models as ttm  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOADS_DIR = BASE_DIR / "data" / "uploads"
UPLOADS_INDEX_PATH = UPLOADS_DIR / "index.json"
CUSTOM_RUNS_DIR = BASE_DIR / "models" / "custom_runs"

REQUIRED_COLUMNS = ttm.NUMERIC_FEATURES + ttm.CATEGORICAL_FEATURES + [ttm.TARGET_COL]
MIN_ROWS = 20
MIN_ROWS_PER_CLASS = 6


# --------------------------------------------------------------------------
# Uploads index -- data/uploads/index.json, a flat list of upload records.
# --------------------------------------------------------------------------
def _load_uploads_index() -> list:
    if not UPLOADS_INDEX_PATH.exists():
        return []
    try:
        return json.loads(UPLOADS_INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_uploads_index(index: list) -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_INDEX_PATH.write_text(json.dumps(index, indent=2), encoding="utf-8")


def list_uploads() -> list:
    return _load_uploads_index()


def get_upload(upload_id: str) -> "dict | None":
    return next((e for e in _load_uploads_index() if e["upload_id"] == upload_id), None)


# --------------------------------------------------------------------------
# Validation -- same schema train_tree_models.py's FEATURE_COLUMNS/TARGET_COL
# require. case_id is NOT required (synthesized if absent) since it's an
# identifier, not a feature.
# --------------------------------------------------------------------------
def validate_dataset(df: pd.DataFrame) -> dict:
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    result = {
        "ok": not missing,
        "missing_columns": missing,
        "required_columns": REQUIRED_COLUMNS,
        "row_count": int(len(df)),
    }
    if missing:
        result["error"] = "Missing required column(s): " + ", ".join(missing)
        return result

    outcome = pd.to_numeric(df[ttm.TARGET_COL], errors="coerce")
    if outcome.isna().any():
        result["ok"] = False
        result["error"] = f"'{ttm.TARGET_COL}' must be numeric 0/1 -- found non-numeric values"
        return result
    unique_vals = sorted(outcome.unique().tolist())
    if not set(unique_vals).issubset({0, 1}):
        result["ok"] = False
        result["error"] = f"'{ttm.TARGET_COL}' must be binary 0/1 -- found values {unique_vals}"
        return result

    class_counts = {str(int(k)): int(v) for k, v in outcome.value_counts().items()}
    result["class_counts"] = class_counts
    if len(df) < MIN_ROWS or len(class_counts) < 2 or min(class_counts.values()) < MIN_ROWS_PER_CLASS:
        result["ok"] = False
        result["error"] = (
            f"Not enough rows for a reliable stratified 70/15/15 split -- need at least "
            f"{MIN_ROWS} rows with at least {MIN_ROWS_PER_CLASS} per class "
            f"(got {len(df)} rows, class counts {class_counts})."
        )
        return result

    return result


# --------------------------------------------------------------------------
# Upload -- saves the raw CSV under data/uploads/, never touching
# data/train.csv or data/holdout.csv.
# --------------------------------------------------------------------------
def save_upload(file_storage) -> dict:
    upload_id = uuid.uuid4().hex[:12]
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file_storage.filename or "dataset.csv").name
    dest_path = UPLOADS_DIR / f"{upload_id}_{safe_name}"
    file_storage.save(dest_path)

    try:
        df = pd.read_csv(dest_path)
    except Exception as e:
        dest_path.unlink(missing_ok=True)
        return {"ok": False, "error": f"Could not parse CSV: {e}"}

    validation = validate_dataset(df)
    entry = {
        "upload_id": upload_id,
        "filename": safe_name,
        "path": str(dest_path.relative_to(BASE_DIR)),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "row_count": int(len(df)),
        "valid": validation["ok"],
        "validation": validation,
        "trained": False,
        "last_run": None,
    }
    index = _load_uploads_index()
    index.insert(0, entry)
    _save_uploads_index(index)
    return entry


def _mark_upload_trained(upload_id: str, run_meta: dict) -> None:
    index = _load_uploads_index()
    for entry in index:
        if entry["upload_id"] == upload_id:
            entry["trained"] = True
            entry["last_run"] = run_meta
            break
    _save_uploads_index(index)


# --------------------------------------------------------------------------
# Custom training -- same steps train_tree_models.main() runs (tune ->
# cross-validate -> fit -> evaluate -> report), against one uploaded CSV.
# Writes to models/custom_runs/<upload_id>/, completely separate from the
# official models/artifacts/ + models/model_report.md.
# --------------------------------------------------------------------------
def run_custom_training(upload_id: str) -> dict:
    entry = get_upload(upload_id)
    if entry is None:
        raise ValueError(f"Unknown upload_id={upload_id!r}")

    dest_path = BASE_DIR / entry["path"]
    df = pd.read_csv(dest_path)
    validation = validate_dataset(df)
    if not validation["ok"]:
        raise ValueError(validation.get("error") or "Dataset failed validation")

    df = df.copy()
    if "case_id" not in df.columns:
        df["case_id"] = [f"custom_{upload_id}_{i}" for i in range(len(df))]
    df["is_peak_execution_window"] = df["is_peak_execution_window"].astype(int)

    run_dir = CUSTOM_RUNS_DIR / upload_id
    run_dir.mkdir(parents=True, exist_ok=True)

    np.random.seed(ttm.RANDOM_SEED)
    train_df, val_df, test_df, train_val_df = ttm.make_split(df)
    split_sizes = {"train": len(train_df), "val": len(val_df), "test": len(test_df)}

    print(f"Custom dataset: {entry['filename']} ({len(df)} rows)")
    print(f"Split -> {split_sizes}")

    print("\n[1/4] Tuning logistic regression (C) on validation set ...")
    logreg_c, _ = ttm.tune_logreg(train_df, val_df)

    print("\n[2/4] Tuning regularized XGBoost on validation set (early stopping on log loss) ...")
    xgb_params, xgb_n_estimators, _ = ttm.tune_xgb(train_df, val_df)

    print("\n[3/4] Running 5-fold stratified CV on train+val for both models ...")
    cv_summary, overfitting_flag = ttm.cross_validate(train_val_df, logreg_c, xgb_params, xgb_n_estimators)

    print("\n[4/4] Refitting on train+val and evaluating ONCE on held-out test ...")
    logreg_pipeline, xgb_pipeline = ttm.fit_final_pipelines(train_val_df, logreg_c, xgb_params, xgb_n_estimators)

    logreg_metrics, logreg_proba, y_test = ttm.evaluate_on_test(logreg_pipeline, test_df, "logreg")
    xgb_metrics, xgb_proba, _ = ttm.evaluate_on_test(xgb_pipeline, test_df, "xgboost")

    train_prior = train_df[ttm.TARGET_COL].mean()
    naive_brier = float(np.mean((train_prior - y_test) ** 2))
    print(f"  LogReg test: {logreg_metrics}")
    print(f"  XGBoost test: {xgb_metrics}")
    print(f"  Naive baseline (constant={train_prior:.3f}) test Brier: {naive_brier:.4f}")

    if xgb_metrics["brier"] < logreg_metrics["brier"]:
        primary_key = "xgboost"
        primary_proba = xgb_proba
    else:
        primary_key = "logreg"
        primary_proba = logreg_proba
    print(f"  -> primary model selected by test Brier score: {primary_key}")

    calib_df = ttm.calibration_table(primary_proba, y_test, n_bins=4)

    run_date = date.today().isoformat()
    logreg_version = f"logreg_custom_{upload_id}_{run_date}"
    xgb_version = f"xgb_custom_{upload_id}_{run_date}"
    primary_version = xgb_version if primary_key == "xgboost" else logreg_version

    joblib.dump(logreg_pipeline, run_dir / "logreg_pipeline.joblib")
    joblib.dump(xgb_pipeline, run_dir / "xgb_pipeline.joblib")

    report_md = ttm.build_report_markdown(
        cv_summary, overfitting_flag, logreg_metrics, xgb_metrics, naive_brier,
        primary_key, primary_version, calib_df, logreg_c, xgb_params, xgb_n_estimators, split_sizes,
    )
    report_md = report_md.replace(
        "# Phase 3 Model Report",
        f"# Custom Dataset Model Report -- {entry['filename']}",
        1,
    )
    (run_dir / "report.md").write_text(report_md, encoding="utf-8")

    metadata = {
        "upload_id": upload_id,
        "filename": entry["filename"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "split_sizes": split_sizes,
        "primary_model": primary_version,
        "primary_model_key": primary_key,
        "models": {
            "logreg": {
                "version": logreg_version,
                "cv_f1_mean": cv_summary["logreg"]["f1_mean"],
                "cv_brier_mean": cv_summary["logreg"]["brier_mean"],
                "test_metrics": logreg_metrics,
            },
            "xgboost": {
                "version": xgb_version,
                "cv_f1_mean": cv_summary["xgboost"]["f1_mean"],
                "cv_brier_mean": cv_summary["xgboost"]["brier_mean"],
                "test_metrics": xgb_metrics,
            },
        },
        "naive_baseline_test_brier": naive_brier,
        "overfitting_flag": overfitting_flag,
    }
    (run_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    print(f"\nWrote {run_dir / 'report.md'}")
    print(f"Done. primary model = {primary_version}")

    _mark_upload_trained(upload_id, metadata)
    return {"report_md": report_md, "metadata": metadata}


def get_custom_report(upload_id: str) -> "dict | None":
    run_dir = CUSTOM_RUNS_DIR / upload_id
    report_path = run_dir / "report.md"
    metadata_path = run_dir / "metadata.json"
    if not report_path.exists():
        return None
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else None
    return {"report_md": report_path.read_text(encoding="utf-8"), "metadata": metadata}
