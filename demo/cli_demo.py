"""
Phase 8 -- CLI Demo.

Reads Phase 1-7 outputs (models/model_report.md, logs/audit_log.csv,
demo/showcase_cases.txt) and prints a plain-text, fixed-width pitch demo.

Makes NO live LLM API calls. The LLM outputs shown for the showcase cases
are the ones already recorded in logs/audit_log.csv by pipeline/run_batch.py
(a real Gemini run -- see PHASE7_REPORT.md) -- this script only reads that
file, it never re-invokes pipeline/llm_layer.py against a live provider.
The one exception is the optional resilience demonstration (see
DEMO_SIMULATE_LLM_FAILURE below), which calls the existing, unmodified
pipeline/llm_layer.get_llm_decision() with a local always-failing adapter --
still no network call, since the fake adapter raises immediately.

Run:
    python demo/cli_demo.py

Optional env var:
    DEMO_SIMULATE_LLM_FAILURE=1  -- also prints a section proving the
    pipeline degrades to requires_human_review=True (never crashes) when
    the LLM adapter throws on every attempt. Use this to rehearse the
    failure path before presenting.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
PIPELINE_DIR = BASE_DIR / "pipeline"
DEMO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPELINE_DIR))

import llm_layer  # noqa: E402  -- only used for the optional failure demo below
import run_batch  # noqa: E402  -- reused for compute_simulation / compute_compliance_check

MODEL_REPORT_PATH = BASE_DIR / "models" / "model_report.md"
AUDIT_LOG_PATH = BASE_DIR / "logs" / "audit_log.csv"
SHOWCASE_PATH = DEMO_DIR / "showcase_cases.txt"

WIDTH = 78

SHOWCASE_TITLES = {
    "tree_only": "TREE-ONLY PATH  (never routed to the LLM)",
    "llm_legible": "LLM-INVOKED PATH  (SHAP-grounded reasoning)",
    "guardrail_override": "GUARDRAIL OVERRIDE  (compliance rule wins)",
    "human_review": "ESCALATED TO HUMAN REVIEW",
}
SHOWCASE_ORDER = ["tree_only", "llm_legible", "guardrail_override", "human_review"]

_BOOL_COLUMNS = ["routed_to_llm", "guardrail_overrode", "requires_human_review", "pipeline_retried", "baseline_retried"]


def _coerce_bool(series: pd.Series) -> pd.Series:
    if series.dtype == bool:
        return series
    return series.astype(str).str.strip().str.lower().eq("true")


def load_audit_log() -> pd.DataFrame:
    if not AUDIT_LOG_PATH.exists():
        print(f"ERROR: {AUDIT_LOG_PATH} does not exist -- run pipeline/run_batch.py first.")
        sys.exit(1)
    df = pd.read_csv(AUDIT_LOG_PATH)
    for col in _BOOL_COLUMNS:
        df[col] = _coerce_bool(df[col])
    return df


def hr(ch: str = "-") -> str:
    return ch * WIDTH


# --------------------------------------------------------------------------
# a. Architecture summary
# --------------------------------------------------------------------------
def print_architecture_summary() -> None:
    print(hr("="))
    print("RAZORPAY AI BUILDATHON -- FAILED-PAYMENT RECOVERY PIPELINE")
    print(hr("="))
    print()
    for line in textwrap.wrap(
        "Architecture: a tree model (logistic regression, SHAP-explained) scores "
        "every declined-payment case -> a confidence gate routes ambiguous cases "
        "(uncertain score band or a domain-flagged decline code) to an LLM, and "
        "resolves clear cases with a template action -> the LLM returns a "
        "structured, schema-validated recommendation with SHAP-grounded reasoning "
        "for the cases it sees -> a deterministic guardrail layer runs on EVERY "
        "case regardless of path and can override the proposed action (hard "
        "declines, NPCI/network retry caps, low confidence) -> every case is "
        "logged to an audit trail with its full decision chain.",
        width=WIDTH,
    ):
        print(line)
    print()


# --------------------------------------------------------------------------
# b. Model comparison table
# --------------------------------------------------------------------------
def parse_model_report_table(path: Path) -> tuple[list[str], list[list[str]]]:
    if not path.exists():
        raise RuntimeError(f"{path} does not exist -- run models/train_tree_models.py first.")
    lines = path.read_text(encoding="utf-8").splitlines()

    start = next((i for i, l in enumerate(lines) if l.strip() == "## LogReg vs XGBoost"), None)
    if start is None:
        raise RuntimeError(f"'## LogReg vs XGBoost' section not found in {path}")

    i = start + 1
    while i < len(lines) and not lines[i].strip().startswith("|"):
        i += 1
    table_lines = []
    while i < len(lines) and lines[i].strip().startswith("|"):
        table_lines.append(lines[i].strip())
        i += 1
    if len(table_lines) < 3:
        raise RuntimeError(f"No usable markdown table found under 'LogReg vs XGBoost' in {path}")

    rows = [[c.strip() for c in line.strip("|").split("|")] for line in table_lines]
    header, body = rows[0], rows[2:]  # rows[1] is the '---' separator
    return header, body


def print_model_comparison_table() -> None:
    header, body = parse_model_report_table(MODEL_REPORT_PATH)
    test_rows = [r for r in body if r[0].startswith("Test ")]
    if not test_rows:
        raise RuntimeError(
            f"No 'Test <metric>' rows (Precision/Recall/F1/Brier) found in the LogReg vs XGBoost "
            f"table in {MODEL_REPORT_PATH} -- table format may have changed."
        )

    rows = [header] + test_rows
    widths = [max(len(r[c]) for r in rows) for c in range(len(header))]

    def fmt_row(r: list[str]) -> str:
        return "  ".join(cell.ljust(widths[c]) for c, cell in enumerate(r))

    print(hr("="))
    print("MODEL COMPARISON  (models/model_report.md)")
    print(hr("="))
    print(fmt_row(header))
    print(hr())
    for r in test_rows:
        print(fmt_row(r))
    verdict_line = next((l for l in MODEL_REPORT_PATH.read_text(encoding="utf-8").splitlines() if l.startswith("**PRIMARY_MODEL")), None)
    if verdict_line:
        print()
        for line in textwrap.wrap(verdict_line.replace("**", ""), width=WIDTH):
            print(line)
    print()


# --------------------------------------------------------------------------
# c. Showcase cases
# --------------------------------------------------------------------------
def load_showcase_map() -> dict[str, str]:
    if not SHOWCASE_PATH.exists():
        raise RuntimeError(f"{SHOWCASE_PATH} does not exist -- run demo/select_showcase_cases.py first.")
    mapping = {}
    for line in SHOWCASE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        label, _, case_id = line.partition("=")
        mapping[label.strip()] = case_id.strip()
    missing = [label for label in SHOWCASE_ORDER if label not in mapping]
    if missing:
        raise RuntimeError(f"{SHOWCASE_PATH} is missing label(s) {missing} -- re-run demo/select_showcase_cases.py.")
    return mapping


def print_showcase_case(row: pd.Series, title: str) -> None:
    print(hr("="))
    print(f"SHOWCASE CASE: {title}")
    print(hr("="))
    print(f"  case_id:            {row.case_id}")
    print(f"  decline_code:       {row.decline_code}")
    print(f"  amount:             Rs {row.amount:,.2f}")
    print(f"  tree_model_score:   {row.tree_model_score:.4f}")

    rationale = row.get("routing_rationale") if hasattr(row, "get") else None
    if rationale and pd.notna(rationale):
        print("  Routing rationale:")
        for line in textwrap.wrap(str(rationale), width=WIDTH - 4):
            print(f"    {line}")

    feats = json.loads(row.tree_model_top_features)[:3]
    print("  SHAP top-3 features:")
    for f in feats:
        print(f"    - {f['feature']:<26s} value={str(f['value']):<14s} shap={f['shap_value']:+.4f}")

    if bool(row.routed_to_llm):
        print(f"  LLM invoked:        yes ({row.model_version})")
        conf = row.llm_confidence
        print(f"  LLM recommended:    {row.llm_recommended_action}  (confidence {conf:.2f})")
        print("  LLM reasoning_summary:")
        for line in textwrap.wrap(str(row.llm_reasoning_summary), width=WIDTH - 4):
            print(f"    {line}")
    else:
        print("  LLM invoked:        no (resolved by tree-model template action)")

    flags = row.guardrail_flags if pd.notna(row.guardrail_flags) and str(row.guardrail_flags).strip() else None
    if bool(row.guardrail_overrode):
        print(f"  Guardrail verdict:  OVERRIDDEN -- rule '{row.override_rule}' fired")
        print(f"    proposed_action -> final_action:  {row.proposed_action}  ->  {row.final_action}")
        if flags and flags != row.override_rule:
            print(f"    all guardrail flags fired:  {flags}")
    else:
        if flags:
            print(f"  Guardrail verdict:  passed (flag(s) recorded but did not override: {flags})")
        else:
            print("  Guardrail verdict:  passed")

    print(f"  requires_human_review: {bool(row.requires_human_review)}")
    print(f"  FINAL ACTION:       {row.final_action}")
    print()


def print_showcase_cases(audit_df: pd.DataFrame) -> dict[str, pd.Series]:
    showcase_map = load_showcase_map()
    rows: dict[str, pd.Series] = {}
    for label in SHOWCASE_ORDER:
        case_id = showcase_map[label]
        match = audit_df[audit_df["case_id"] == case_id]
        if match.empty:
            raise RuntimeError(f"case_id {case_id!r} (label {label!r}) not found in {AUDIT_LOG_PATH}")
        row = match.iloc[0]
        rows[label] = row
        print_showcase_case(row, SHOWCASE_TITLES[label])
    return rows


# --------------------------------------------------------------------------
# Resilience demo: simulated LLM failure (DEMO_SIMULATE_LLM_FAILURE=1)
# --------------------------------------------------------------------------
class _AlwaysFailAdapter(llm_layer.LLMAdapter):
    """Test-only adapter: raises on every call, regardless of input. Used to
    prove pipeline/llm_layer.get_llm_decision() (unmodified) degrades to
    requires_human_review=True instead of crashing the batch. No network
    call is made -- this never touches ClaudeAdapter/GeminiAdapter."""

    def generate(self, case: dict) -> dict:
        raise RuntimeError("Simulated LLM outage (DEMO_SIMULATE_LLM_FAILURE=1)")


def print_llm_failure_resilience_demo(sample_row: pd.Series) -> None:
    print(hr("="))
    print("RESILIENCE CHECK  (DEMO_SIMULATE_LLM_FAILURE=1)")
    print(hr("="))
    print("  Forcing every LLM call to raise, for the LLM-invoked showcase case above,")
    print("  using pipeline/llm_layer.get_llm_decision() unmodified -- only the adapter")
    print("  passed in is a local always-failing stand-in.")
    print()

    case_payload = {
        "case_id": sample_row.case_id,
        "tree_model_score": sample_row.tree_model_score,
        "shap_top_features": json.loads(sample_row.tree_model_top_features),
        "case_facts": {"decline_code": sample_row.decline_code, "amount": sample_row.amount},
    }
    result = llm_layer.get_llm_decision(_AlwaysFailAdapter(), case_payload)

    print(f"  Adapter raised on all {llm_layer.MAX_GENERATE_ATTEMPTS} attempt(s). Pipeline did NOT crash.")
    print(f"    recommended_action:     {result['recommended_action']}")
    print(f"    requires_human_review:  {result['requires_human_review']}")
    print(f"    model_version:          {result['model_version']}")
    print(f"    reasoning_summary:      {result['reasoning_summary']}")
    assert result["requires_human_review"] is True, "resilience invariant violated: expected requires_human_review=True"
    assert result["recommended_action"] == "escalate_human", "resilience invariant violated: expected escalate_human fallback"
    print()
    print("  Invariant held: requires_human_review=True, recommended_action=escalate_human.")
    print()


# --------------------------------------------------------------------------
# d. Headline recovered-amount number -- three scenarios, not two.
#
# A raw pipeline-vs-"naive baseline" $ comparison is misleading unless the
# baseline is itself guardrailed: an UNguardrailed "retry everyone" policy
# recovers more raw $ partly BECAUSE it executes retries against hard
# declines / NPCI-capped cases that compliance rules forbid -- money the
# real pipeline could never legally chase. So three scenarios are shown
# side by side: a truly naive policy (no rule checks, with its compliance-
# violation count made explicit), a guardrailed-but-untargeted policy
# (isolates what respecting the rules costs/gains on its own), and the
# actual pipeline (isolates what ML/LLM targeting adds on top of that).
# See run_batch.compute_three_scenario_simulation for the computation.
# --------------------------------------------------------------------------
def print_headline_numbers(audit_df: pd.DataFrame) -> None:
    sim = run_batch.compute_three_scenario_simulation(audit_df)
    compliance = run_batch.compute_compliance_check(audit_df)

    naive = sim["naive_no_guardrails"]
    compliant = sim["compliant_no_targeting"]
    pipeline = sim["pipeline"]

    print(hr("="))
    print("HEADLINE RESULT  (three scenarios, computed live from logs/audit_log.csv)")
    print(hr("="))
    print(f"  Batch size:                  {sim['n_cases']} cases, Rs {sim['total_amount']:,.2f} total at stake")
    print(f"  Cost per retry attempt:      Rs {sim['cost_per_retry_attempt']:,.2f}  (assumption, see run_batch.py)")
    print()

    # Column widths kept tight so the row stays within WIDTH (78) for a
    # no-color, fixed-width, projector-safe report.
    LABEL_W, ATT_W, NET_W, PCT_W, EFF_W, VIOL_W = 25, 8, 11, 6, 9, 6
    rows_data = [
        ("Naive (no guardrails)", naive),
        ("Compliant (guardrails)", compliant),
        ("Pipeline (guard.+ML/LLM)", pipeline),
    ]
    header = (
        f"  {'Scenario':<{LABEL_W}s} {'Attempts':>{ATT_W}s} {'Net Rs':>{NET_W}s} "
        f"{'Net %':>{PCT_W}s} {'Rs/attpt':>{EFF_W}s} {'Viol.':>{VIOL_W}s}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for label, s in rows_data:
        print(
            f"  {label:<{LABEL_W}s} {s['retry_attempts']:>{ATT_W}d} "
            f"{s['net_recovered']:>{NET_W},.2f} {s['net_recovered_pct']:>{PCT_W - 1}.1f}% "
            f"{s['recovered_rs_per_attempt']:>{EFF_W},.2f} {s['compliance_violations']:>{VIOL_W}d}"
        )
    print()
    for line in textwrap.wrap(
        "\"Naive\" has zero rule checks -- its Viol. count is how many of its "
        f"{naive['retry_attempts']} retries would fire a hard-decline/NPCI-cap/network-cap "
        "guardrail (npci_retry_cap_reached, hard_decline_excluded, etc). Its higher raw Net Rs "
        "is not a fair headline number on its own -- see Rs/attpt below.",
        width=WIDTH - 2,
    ):
        print(f"  {line}")
    print()

    lift_naive = sim["lift_pipeline_vs_naive_no_guardrails"]
    lift_compliant = sim["lift_pipeline_vs_compliant_no_targeting"]
    sign_naive = "improvement" if lift_naive["absolute"] >= 0 else "shortfall"
    sign_compliant = "improvement" if lift_compliant["absolute"] >= 0 else "shortfall"

    for line in textwrap.wrap(
        f"Net lift, pipeline vs. NON-COMPLIANT naive baseline: Rs {lift_naive['absolute']:,.2f} "
        f"({lift_naive['pct']:.1f}%) [{sign_naive}] -- NOT apples-to-apples, see Viol. column above.",
        width=WIDTH - 2,
    ):
        print(f"  {line}")
    print()
    for line in textwrap.wrap(
        f"Net lift, pipeline vs. compliant baseline (isolates what ML/LLM targeting adds "
        f"on top of guardrails alone): Rs {lift_compliant['absolute']:,.2f} ({lift_compliant['pct']:.1f}%) "
        f"[{sign_compliant}]",
        width=WIDTH - 2,
    ):
        print(f"  {line}")
    print()

    eff_vs_naive_pct = (pipeline["recovered_rs_per_attempt"] / naive["recovered_rs_per_attempt"] - 1.0) * 100.0 if naive["recovered_rs_per_attempt"] else float("nan")
    eff_vs_compliant_pct = (pipeline["recovered_rs_per_attempt"] / compliant["recovered_rs_per_attempt"] - 1.0) * 100.0 if compliant["recovered_rs_per_attempt"] else float("nan")
    print("  STRONGEST NUMBER -- recovered-Rs per retry attempt (revenue efficiency):")
    for line in textwrap.wrap(
        f"Pipeline Rs {pipeline['recovered_rs_per_attempt']:,.2f}/attempt is "
        f"{eff_vs_compliant_pct:+.1f}% vs. the compliant baseline (Rs {compliant['recovered_rs_per_attempt']:,.2f}) "
        f"and {eff_vs_naive_pct:+.1f}% vs. the naive baseline (Rs {naive['recovered_rs_per_attempt']:,.2f}).",
        width=WIDTH - 4,
    ):
        print(f"    {line}")
    print()
    print(f"  Overall pipeline compliance: {compliance['non_compliant_count']}/{compliance['n_cases']} non-compliant executions")
    print()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    start = time.perf_counter()

    print_architecture_summary()
    print_model_comparison_table()

    audit_df = load_audit_log()
    showcase_rows = print_showcase_cases(audit_df)

    if os.environ.get("DEMO_SIMULATE_LLM_FAILURE", "").strip() in ("1", "true", "True"):
        print_llm_failure_resilience_demo(showcase_rows["llm_legible"])

    print_headline_numbers(audit_df)

    elapsed = time.perf_counter() - start
    print(hr("="))
    print(f"Demo complete. Elapsed: {elapsed:.2f}s")
    print(hr("="))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
