"""
Phase 7 -- Validation checks for logs/audit_log.csv.

Plain-assert checks, runnable either directly:
    python pipeline/validate_audit_log.py
or via pytest:
    python -m pytest pipeline/validate_audit_log.py -v

Run AFTER pipeline/run_batch.py has produced logs/audit_log.csv.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

import run_batch
import shap_extract
from schema import RECOMMENDED_ACTIONS

BASE_DIR = Path(__file__).resolve().parent.parent
AUDIT_LOG_PATH = BASE_DIR / "logs" / "audit_log.csv"

ROUTED_TO_LLM_MIN = 0.15
ROUTED_TO_LLM_MAX = 0.5


def _coerce_bool(series: pd.Series) -> pd.Series:
    """CSV round-trips bool columns as native bool dtype when every value is
    True/False (pandas infers this automatically), but coerce explicitly in
    case the file was hand-edited or a value is missing."""
    if series.dtype == bool:
        return series
    return series.astype(str).str.strip().str.lower().eq("true")


_BOOL_COLUMNS = [
    "routed_to_llm",
    "guardrail_overrode",
    "baseline_guardrail_overrode",
    "pipeline_retried",
    "baseline_retried",
]


def load_audit_log() -> pd.DataFrame:
    df = pd.read_csv(AUDIT_LOG_PATH)
    for col in _BOOL_COLUMNS:
        df[col] = _coerce_bool(df[col])
    return df


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
def check_row_count(audit_df: pd.DataFrame, input_row_count: int) -> None:
    assert len(audit_df) == input_row_count, (
        f"Audit log row count ({len(audit_df)}) != input batch row count ({input_row_count})"
    )


def check_final_action_enum(audit_df: pd.DataFrame) -> None:
    valid = audit_df["final_action"].notna() & audit_df["final_action"].isin(RECOMMENDED_ACTIONS)
    bad = audit_df.loc[~valid, ["case_id", "final_action"]]
    assert bad.empty, (
        f"{len(bad)} row(s) have a final_action outside the fixed enum {RECOMMENDED_ACTIONS} "
        f"or null:\n{bad.to_string(index=False)}"
    )


def check_routed_to_llm_ratio(audit_df: pd.DataFrame) -> float:
    mean = float(audit_df["routed_to_llm"].mean())
    assert ROUTED_TO_LLM_MIN <= mean <= ROUTED_TO_LLM_MAX, (
        f"routed_to_llm mean is {mean:.3f}, outside [{ROUTED_TO_LLM_MIN}, {ROUTED_TO_LLM_MAX}] -- "
        "a degenerate split means the confidence gate is broken."
    )
    return mean


def check_guardrail_fired(audit_df: pd.DataFrame) -> int:
    count = int(audit_df["guardrail_overrode"].sum())
    assert count >= 1, "No row has guardrail_overrode == True -- the guardrail layer never fired on this batch."
    return count


def check_net_recovered_absolute(audit_df: pd.DataFrame) -> tuple[str, float, float]:
    """Answers: did the pipeline recover more TOTAL net dollars than the
    naive baseline? Non-fatal, not a hard assertion -- see run_batch.py
    module docstring for why gross recovered-$ is not asserted at all: the
    naive baseline retries a superset of what the pipeline retries, so on
    total dollars (gross or net) a policy that fires far more retry attempts
    can win purely on volume even while being far less efficient per attempt
    (see check_net_recovered_efficiency, the fatal check, for the metric
    that isolates targeting quality from retry volume).

    Returns (status, pipeline_net, baseline_net) instead of printing or
    asserting -- status is "ok" if the pipeline beat baseline on total net
    dollars, "warn" otherwise. main()'s runner loop special-cases this one
    check (by name) to turn that status into exactly one WARN/PASS line,
    since a generic assert-based PASS/FAIL would either hide the shortfall
    or double-print it."""
    sim = run_batch.compute_simulation(audit_df)
    pipeline_net = sim["pipeline_net_recovered"]
    baseline_net = sim["baseline_net_recovered"]
    status = "ok" if pipeline_net > baseline_net else "warn"
    return status, pipeline_net, baseline_net


def check_net_recovered_efficiency(audit_df: pd.DataFrame) -> tuple[float, float]:
    """Headline check -- answers: per retry attempt actually fired, did the
    pipeline recover more revenue than a guardrailed policy with no
    ML/LLM targeting? This isolates targeting quality from retry volume,
    which check_net_recovered_absolute's total-dollar comparison conflates
    (a policy that fires many more attempts can win on total dollars while
    being less efficient per attempt). See
    run_batch.compute_three_scenario_simulation for the compliant_no_targeting
    scenario definition."""
    three_scenario = run_batch.compute_three_scenario_simulation(audit_df)
    pipeline_rpa = three_scenario["pipeline"]["recovered_rs_per_attempt"]
    compliant_rpa = three_scenario["compliant_no_targeting"]["recovered_rs_per_attempt"]
    assert pipeline_rpa > compliant_rpa, (
        f"pipeline recovered_rs_per_attempt (Rs {pipeline_rpa:,.2f}) is not > "
        f"compliant_no_targeting recovered_rs_per_attempt (Rs {compliant_rpa:,.2f})"
    )
    return pipeline_rpa, compliant_rpa


def check_retry_attempts(audit_df: pd.DataFrame) -> tuple[int, int]:
    sim = run_batch.compute_simulation(audit_df)
    pipeline_attempts = sim["pipeline_retry_attempts"]
    baseline_attempts = sim["baseline_retry_attempts"]
    assert pipeline_attempts < baseline_attempts, (
        f"pipeline_retry_attempts ({pipeline_attempts}) is not < "
        f"baseline_retry_attempts ({baseline_attempts})"
    )
    return pipeline_attempts, baseline_attempts


def check_wasted_retries_sanity(audit_df: pd.DataFrame) -> int:
    """Sanity check that the batch actually contains hard-decline / NPCI-
    or network-capped cases for the naive baseline to fail into. If this is
    0, the batch doesn't exercise the scenario the whole net-recovered
    argument depends on -- that's a data problem to flag, not a code
    problem to hide."""
    sim = run_batch.compute_simulation(audit_df)
    count = sim["wasted_retries_on_guaranteed_fails"]
    assert count > 0, (
        "wasted_retries_on_guaranteed_fails is 0 -- the naive baseline never got blocked by a guardrail on this "
        "batch, so it contains no hard-decline / NPCI-cap / network-cap cases to demonstrate the pipeline's "
        "advantage against."
    )
    return count


def check_no_noncompliant_executions(audit_df: pd.DataFrame) -> int:
    """Verifies the "zero uncompliant executions" claim rendered into
    demo/pitch_numbers.md rather than leaving it as unverified prose -- see
    run_batch.compute_compliance_check. Fails loudly (does not silently
    loosen) if a hard-decline or cap-exceeded case ever executed a retry;
    that would indicate guardrails.apply_guardrails isn't actually
    enforcing its own override on the audited final_action."""
    result = run_batch.compute_compliance_check(audit_df)
    assert result["non_compliant_count"] == 0, (
        f"{result['non_compliant_count']} cases executed a retry action despite a hard-decline or "
        f"cap-exceeded guardrail flag: {result['non_compliant_case_ids']}"
    )
    return result["non_compliant_count"]


def print_gross_recovered(audit_df: pd.DataFrame) -> None:
    """Informational only, per design -- NOT asserted (see check_net_recovered)."""
    sim = run_batch.compute_simulation(audit_df)
    print(
        f"INFO  gross_recovered: pipeline=Rs {sim['pipeline_gross_recovered']:,.2f}  "
        f"baseline=Rs {sim['baseline_gross_recovered']:,.2f}  "
        "(pipeline may legitimately be lower or comparable -- it retries fewer cases; not asserted)"
    )


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
_CHECKS = [
    ("row_count", check_row_count),
    ("final_action_enum", check_final_action_enum),
    ("routed_to_llm_ratio", check_routed_to_llm_ratio),
    ("guardrail_fired", check_guardrail_fired),
    ("net_recovered_absolute", check_net_recovered_absolute),
    ("net_recovered_efficiency", check_net_recovered_efficiency),
    ("retry_attempts", check_retry_attempts),
    ("wasted_retries_sanity", check_wasted_retries_sanity),
    ("no_noncompliant_executions", check_no_noncompliant_executions),
]


def main() -> int:
    if not AUDIT_LOG_PATH.exists():
        print(f"FAIL  {AUDIT_LOG_PATH} does not exist -- run `python pipeline/run_batch.py` first.")
        return 1

    audit_df = load_audit_log()
    input_row_count = len(shap_extract.load_batch_df())

    failures = 0
    warnings = 0
    for name, check_fn in _CHECKS:
        # net_recovered_absolute is non-fatal by design (see its docstring)
        # and returns a status instead of asserting, so it can't go through
        # the generic assert-based PASS/FAIL branch below -- that would
        # either swallow the shortfall silently or (if it printed
        # internally, as an earlier version did) double-print alongside
        # this loop's own PASS line.
        if name == "net_recovered_absolute":
            status, pipeline_net, baseline_net = check_fn(audit_df)
            if status == "warn":
                diff = baseline_net - pipeline_net
                pct = (diff / baseline_net * 100.0) if baseline_net else float("nan")
                print(
                    f"WARN  {name}: pipeline is Rs {diff:,.2f} ({pct:.1f}%) behind baseline "
                    "on total net dollars -- see net_recovered_efficiency for the per-attempt comparison"
                )
                warnings += 1
            else:
                print(f"PASS  {name}")
            continue

        try:
            if name == "row_count":
                check_fn(audit_df, input_row_count)
            else:
                check_fn(audit_df)
        except AssertionError as exc:
            print(f"FAIL  {name}: {exc}")
            failures += 1
        else:
            print(f"PASS  {name}")

    print_gross_recovered(audit_df)

    total = len(_CHECKS)
    passed = total - failures - warnings
    print(f"\n{passed} passed, {warnings} warned, {failures} failed (of {total} checks)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
