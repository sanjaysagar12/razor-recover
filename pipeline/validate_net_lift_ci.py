"""
Phase 23 -- Bootstrap CI + breakeven-cost sensitivity for net_lift_pct.

Standalone, re-runnable, informational (not pass/fail) companion to
validate_audit_log.py. Answers two questions the point-estimate net_lift_pct
in run_batch.compute_simulation can't: how much would net_lift_pct move
under resampling of the same batch (bootstrap CI), and at what
COST_PER_RETRY_ATTEMPT value would the pipeline and naive baseline tie on
net recovered-$ (breakeven cost)?

Run with (after pipeline/run_batch.py has produced logs/audit_log.csv):
    python pipeline/validate_net_lift_ci.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

import run_batch

BASE_DIR = Path(__file__).resolve().parent.parent
AUDIT_LOG_PATH = BASE_DIR / "logs" / "audit_log.csv"

_BOOL_COLUMNS = [
    "routed_to_llm",
    "guardrail_overrode",
    "baseline_guardrail_overrode",
    "pipeline_retried",
    "baseline_retried",
]


def _coerce_bool(series: pd.Series) -> pd.Series:
    """CSV round-trips bool columns as native bool dtype when every value is
    True/False (pandas infers this automatically), but coerce explicitly in
    case the file was hand-edited or a value is missing."""
    if series.dtype == bool:
        return series
    return series.astype(str).str.strip().str.lower().eq("true")


def load_audit_log() -> pd.DataFrame:
    df = pd.read_csv(AUDIT_LOG_PATH)
    for col in _BOOL_COLUMNS:
        df[col] = _coerce_bool(df[col])
    return df


# --------------------------------------------------------------------------
# Bootstrap CI
# --------------------------------------------------------------------------
def bootstrap_net_lift_ci(audit_df: pd.DataFrame, n_resamples: int = 5000, seed: int = 42) -> dict:
    """Resamples case rows (with replacement) and recomputes net_lift_pct
    per resample via run_batch.compute_simulation -- the same merge against
    shap_extract.load_batch_df() ground truth and the same
    COST_PER_RETRY_ATTEMPT that function already uses, called fresh on each
    resampled audit_df rather than reimplemented here, so this can't drift
    from compute_simulation's actual logic."""
    point_estimate = run_batch.compute_simulation(audit_df)["net_lift_pct"]

    rng = np.random.default_rng(seed)
    n = len(audit_df)
    resampled_lifts = np.empty(n_resamples, dtype=float)
    for i in range(n_resamples):
        idx = rng.integers(0, n, size=n)
        resampled_lifts[i] = run_batch.compute_simulation(audit_df.iloc[idx])["net_lift_pct"]

    ci_low, ci_high = np.percentile(resampled_lifts, [2.5, 97.5])
    pct_nonnegative = float((resampled_lifts >= 0).mean() * 100.0)

    return {
        "point_estimate": float(point_estimate),
        "ci_low": float(ci_low),
        "ci_high": float(ci_high),
        "pct_resamples_nonnegative": pct_nonnegative,
        "n_resamples": n_resamples,
    }


# --------------------------------------------------------------------------
# Breakeven cost
# --------------------------------------------------------------------------
def compute_breakeven_cost(audit_df: pd.DataFrame) -> float:
    """Solves for the COST_PER_RETRY_ATTEMPT value at which
    pipeline_net_recovered == baseline_net_recovered:

        pipeline_gross - pipeline_attempts*c == baseline_gross - baseline_attempts*c
        c == (baseline_gross - pipeline_gross) / (baseline_attempts - pipeline_attempts)

    Raises ValueError (rather than returning a silent inf/nan) if
    baseline_attempts == pipeline_attempts, since the equation is then
    either unsatisfiable or true for every c."""
    sim = run_batch.compute_simulation(audit_df)
    baseline_attempts = sim["baseline_retry_attempts"]
    pipeline_attempts = sim["pipeline_retry_attempts"]
    if baseline_attempts == pipeline_attempts:
        raise ValueError(
            "baseline_retry_attempts == pipeline_retry_attempts -- breakeven cost is undefined "
            "(division by zero in (baseline_gross - pipeline_gross) / (baseline_attempts - pipeline_attempts))"
        )
    return (sim["baseline_gross_recovered"] - sim["pipeline_gross_recovered"]) / (baseline_attempts - pipeline_attempts)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> int:
    if not AUDIT_LOG_PATH.exists():
        print(f"FAIL  {AUDIT_LOG_PATH} does not exist -- run `python pipeline/run_batch.py` first.")
        return 1

    audit_df = load_audit_log()

    ci = bootstrap_net_lift_ci(audit_df)
    print(f"INFO  net_lift_pct point estimate: {ci['point_estimate']:.1f}%")
    print(
        f"INFO  95% bootstrap CI (n={ci['n_resamples']} resamples): "
        f"[{ci['ci_low']:.1f}%, {ci['ci_high']:.1f}%]"
    )
    print(f"INFO  resamples with net_lift_pct >= 0%: {ci['pct_resamples_nonnegative']:.1f}%")

    try:
        breakeven = compute_breakeven_cost(audit_df)
    except ValueError as exc:
        print(f"INFO  breakeven cost per retry attempt: undefined -- {exc}")
    else:
        print(f"INFO  breakeven cost per retry attempt: Rs {breakeven:,.2f} (current: Rs {run_batch.COST_PER_RETRY_ATTEMPT:,.2f})")

    print("\nThis script is informational only -- it asserts nothing and always exits 0.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
