"""
Phase 7 -- Audit Log + Batch Simulation (Orchestrator).

Runs every case in the batch through the full pipeline -- tree model score
(Phase 3) + SHAP top-5 (Phase 4) -> confidence gate (Phase 4) -> LLM layer
(Phase 5, only for routed cases) -> guardrails (Phase 6, every case,
regardless of path) -- and writes one fully-populated audit row per case to
logs/audit_log.csv. It then runs a batch simulation against the audit log
(not by re-running the pipeline) and writes demo/pitch_numbers.md.

--------------------------------------------------------------------------
Data-source note (same adaptation Phase 3/4 made -- see shap_extract.py)
--------------------------------------------------------------------------
The Phase 7 spec assumes a single `data/synthetic_batch.csv` with a
`would_recover` ground-truth column. The actual Phase 2 output in this repo
is `data/train.csv` + `data/holdout.csv` (see shap_extract.load_batch_df),
whose ground-truth column is named `outcome` (1 = payment recovered on
retry -- see data/generate_synthetic.py). This module reuses that same
280-row batch and treats `outcome == 1` as `would_recover`, so the audit log
and simulation line up 1:1 with the same rows Phase 4/5/6 already operate
on.

--------------------------------------------------------------------------
Why the naive baseline is ALSO guardrailed (batch simulation design note)
--------------------------------------------------------------------------
`outcome` is a fixed, action-independent historical label: it does not
depend on which action a policy takes. That means for any metric of the
form "sum(amount) where policy_retried AND would_recover", a policy that
retries strictly MORE cases can never recover LESS -- retrying every case
unconditionally is a trivial upper bound on gross recovered revenue. An
early version of this simulation compared the pipeline's guardrailed
recoverable-action set against an UNGUARDED "retry everyone" baseline
credited purely off `would_recover`; that baseline set is a strict superset
of the pipeline's by construction (the pipeline only ever retries fewer,
more carefully chosen cases), so "pipeline beats baseline on gross $" was
mathematically impossible regardless of model quality.

The fix: run the naive policy's "retry_now, every case" proposal through
the SAME pipeline/guardrails.py rules as the real pipeline (see
compute_baseline_pass). This is not cosmetic -- those rules encode real
compliance/network constraints (a stolen card will not actually recover no
matter how many times you retry it; NPCI caps retries at 4). A naive policy
does not get to ignore them just because it doesn't reason about them, so
crediting it as if it could is unrealistic. Guarding the baseline narrows
the gross-$ gap but does not close it (the baseline still retries every
case the guardrails allow, a superset of what the pipeline retries) -- so
gross recovered-$ is reported for both but NOT asserted as pipeline >
baseline. What the guardrails DO make legitimately comparable is retry
volume and its cost: the naive policy burns retry attempts (and, per
COST_PER_RETRY_ATTEMPT below, money) on cases the guardrails block outright,
while the pipeline mostly avoids them. NET recovered-$ (gross minus
attempts * COST_PER_RETRY_ATTEMPT) is the metric where the pipeline is
expected to win, and is what validate_audit_log.py actually asserts.

Run with:
    python pipeline/run_batch.py
Then validate with:
    python pipeline/validate_audit_log.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

import confidence_gate
import guardrails
import llm_layer
import shap_extract

BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
DEMO_DIR = BASE_DIR / "demo"
AUDIT_LOG_PATH = LOGS_DIR / "audit_log.csv"
PITCH_NUMBERS_PATH = DEMO_DIR / "pitch_numbers.md"

PIPELINE_VERSION = "phase7_v1"

# Actions that count as "money in motion" for the batch simulation -- the
# only actions that give a case a chance to recover the payment.
RECOVERABLE_ACTIONS = {"retry_now", "retry_scheduled", "prompt_alt_payment"}

# ASSUMPTION, not derived from data: flat cost (in the same currency unit as
# `amount`) charged per retry attempt executed, regardless of outcome --
# gateway/network processing fee, issuer soft-decline friction, etc. Tune
# this to whatever real per-attempt cost figure is available; it only
# affects the NET recovered-$ comparison (compute_simulation), not the
# gross figures or the audit log rows themselves.
COST_PER_RETRY_ATTEMPT = 10.0

AUDIT_COLUMNS = [
    "case_id",
    "timestamp",
    "decline_code",
    "amount",
    "tree_model_score",
    "routing_rationale",
    "tree_model_top_features",
    "routed_to_llm",
    "llm_recommended_action",
    "llm_confidence",
    "llm_reasoning_summary",
    "llm_schema_valid",
    "guardrail_flags",
    "proposed_action",
    "final_action",
    "guardrail_overrode",
    "override_rule",
    "requires_human_review",
    "model_version",
    "pipeline_version",
    # -- naive-baseline columns (see module docstring: "Why the naive
    # baseline is ALSO guardrailed") -- computed alongside the pipeline's
    # own row so compute_simulation can work purely off the audit log.
    "baseline_final_action",
    "baseline_guardrail_overrode",
    "baseline_override_rule",
    "pipeline_retried",
    "baseline_retried",
]


# --------------------------------------------------------------------------
# Confidence-gate routing transparency (Phase 8 follow-up)
# --------------------------------------------------------------------------
def _routing_rationale(
    tree_score: float,
    decline_code: str,
    band_low: float,
    band_high: float,
    routing_trigger: list[str],
    routed_to_llm: bool,
    template_action: Optional[str],
    ambiguous_code_flag: bool,
) -> str:
    """Human-readable explanation of confidence_gate.route_case()'s routing
    decision for one case. band_low/band_high feed _in_probability_band
    (a pure function of the band -- safe to recompute, can't drift).
    ambiguous_code_flag is NOT recomputed here -- it's read straight from
    route_case()'s own return value (record["ambiguous_code_flag"]),
    because route_case can be told the real ambiguous-code answer by a
    caller (e.g. webhook_receiver.py, via decline_code_mapper) that
    overrides the legacy prefix-match confidence_gate._is_ambiguous_code
    would otherwise give. Recomputing it independently here (the previous
    version of this function did, via confidence_gate._is_ambiguous_code)
    silently ignores that override and can render an explanation that
    contradicts the actual routing decision -- see PHASE7_REPORT.md-era bug:
    a real case routed via ambiguous_code_flag=True from decline_code_mapper
    still printed 'ambiguous_code=False' in this string, because the prefix
    match on a real Razorpay error_reason like 'generic_decline' never
    matches confidence_gate.AMBIGUOUS_DECLINE_CODES' synthetic prefixes."""
    in_band = confidence_gate._in_probability_band(tree_score, band_low, band_high)

    band_clause = f"score={tree_score:.4f} vs band=[{band_low:.4f}, {band_high:.4f}] (in_band={in_band})"
    code_clause = f"decline_code={decline_code!r} (is_ambiguous={ambiguous_code_flag})"

    if routed_to_llm:
        trigger_str = "+".join(routing_trigger) if routing_trigger else "unknown"
        return f"{band_clause}; {code_clause} -> ROUTED to LLM (trigger: {trigger_str})"
    return f"{band_clause}; {code_clause} -> NOT routed (template_action={template_action})"


# --------------------------------------------------------------------------
# Per-case row assembly
# --------------------------------------------------------------------------
def _resolve_override_rule(case: dict, proposed: dict, guardrail_flags: list[str]) -> Optional[str]:
    """Which rule's override actually changed final_action, mirroring
    apply_guardrails' own "first rule in GUARDRAIL_RULES order, present in
    guardrail_flags, whose override touches final_action" logic -- without
    duplicating guardrails.py's rule definitions here. Used for both the
    pipeline's own override and the naive baseline's."""
    for name, _condition, override in guardrails.GUARDRAIL_RULES:
        if name not in guardrail_flags:
            continue
        if "final_action" in override(case, proposed):
            return name
    return None


def _build_template_proposed(record: dict, shap_top: list[dict], tree_model_version: str) -> dict:
    """proposed dict for a case NOT routed to the LLM -- the tree model's
    template_action (Phase 4), shaped to match what apply_guardrails expects
    from an LLM decision so both paths flow through the same guardrail call."""
    return {
        "case_id": record["case_id"],
        "tree_model_score": record["tree_model_score"],
        "tree_model_top_features": [{"feature": f["feature"], "shap_value": f["shap_value"]} for f in shap_top],
        "recommended_action": record["template_action"],
        "action_scheduled_for": None,
        "confidence": record["tree_model_score"],
        "reasoning_summary": f"Tree-model template action (routing_trigger={record['routing_trigger']}).",
        "guardrail_flags": [],
        "requires_human_review": False,
        "model_version": tree_model_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _build_naive_baseline_proposed(case_id: str) -> dict:
    """The naive "retry every case once" policy's proposal, before
    guardrails: always retry_now, immediately (no scheduling -- so it can
    never trip npci_peak_window, which only fires on a scheduled retry), at
    full (uninformed) confidence so it never trips requires_human_review_floor
    on its own -- a blind policy isn't hedging, it just always retries. The
    only guardrails that can realistically catch it are the case-fact-only
    ones: hard_decline_excluded and network/NPCI retry caps."""
    return {
        "case_id": case_id,
        "recommended_action": "retry_now",
        "action_scheduled_for": None,
        "confidence": 1.0,
        "reasoning_summary": "Naive baseline policy: retry every case once.",
        "guardrail_flags": [],
        "requires_human_review": False,
        "model_version": "naive_baseline_v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_audit_row(
    record: dict,
    shap_top: list[dict],
    case_facts: dict,
    tree_model_version: str,
    adapter: "llm_layer.LLMAdapter | None",
    band_low: float,
    band_high: float,
) -> dict:
    """record: one confidence_gate.route_case() result (case_id,
    tree_model_score, routed_to_llm, routing_trigger, template_action).
    shap_top: this case's SHAP top-5 (shap_extract.get_shap_top_features
    shape). case_facts: shap_extract.get_case_facts(case_id). band_low/
    band_high: this batch's confidence-gate probability band (see
    confidence_gate.compute_probability_band), passed through only so
    _routing_rationale can explain the routing decision -- routing itself
    was already decided upstream in confidence_gate.route_case()."""
    case_id = record["case_id"]
    routed_to_llm = record["routed_to_llm"]
    routing_rationale = _routing_rationale(
        record["tree_model_score"],
        case_facts["decline_code"],
        band_low,
        band_high,
        record["routing_trigger"],
        routed_to_llm,
        record["template_action"],
        record["ambiguous_code_flag"],
    )

    if routed_to_llm:
        case_payload = {
            "case_id": case_id,
            "tree_model_score": record["tree_model_score"],
            "shap_top_features": shap_top,
            "case_facts": case_facts,
        }
        llm_output = llm_layer.get_llm_decision(adapter, case_payload)
        llm_schema_valid = "llm_validation_error" not in llm_output
        if not llm_schema_valid:
            # get_llm_decision's fallback already sets this True; forced
            # again here so the invariant holds even if that ever changes.
            llm_output["requires_human_review"] = True

        proposed = llm_output
        llm_recommended_action = llm_output.get("recommended_action")
        llm_confidence = llm_output.get("confidence")
        llm_reasoning_summary = llm_output.get("reasoning_summary")
        model_version = llm_output.get("model_version", tree_model_version)
    else:
        proposed = _build_template_proposed(record, shap_top, tree_model_version)
        llm_schema_valid = None
        llm_recommended_action = None
        llm_confidence = None
        llm_reasoning_summary = None
        model_version = tree_model_version

    case_dict = {
        "case_id": case_id,
        "decline_code": case_facts["decline_code"],
        "payment_rail": case_facts["payment_rail"],
        "retry_attempt_number": case_facts["retry_attempt_number"],
        "cumulative_retries_this_txn": case_facts["cumulative_retries_this_txn"],
    }

    # -- Pipeline's own guardrail pass -------------------------------------
    guardrail_result = guardrails.apply_guardrails(case_dict, proposed)

    proposed_action = guardrail_result["proposed_action"]
    final_action = guardrail_result["final_action"]
    guardrail_flags = guardrail_result["guardrail_flags"]
    guardrail_overrode = final_action != proposed_action
    override_rule = _resolve_override_rule(case_dict, proposed, guardrail_flags) if guardrail_overrode else None

    # -- Naive baseline's guardrail pass (same case_dict, same rules -- see
    # module docstring) ----------------------------------------------------
    baseline_proposed = _build_naive_baseline_proposed(case_id)
    baseline_result = guardrails.apply_guardrails(case_dict, baseline_proposed)
    baseline_final_action = baseline_result["final_action"]
    baseline_guardrail_flags = baseline_result["guardrail_flags"]
    baseline_guardrail_overrode = baseline_final_action != baseline_result["proposed_action"]
    baseline_override_rule = (
        _resolve_override_rule(case_dict, baseline_proposed, baseline_guardrail_flags)
        if baseline_guardrail_overrode
        else None
    )

    return {
        "case_id": case_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # Not one of AUDIT_COLUMNS -- pd.DataFrame(rows, columns=AUDIT_COLUMNS)
        # in run_batch() below silently drops it, so logs/audit_log.csv is
        # unaffected. Surfaced only for webhook_receiver.py's execute_action,
        # which needs the proposal's scheduled time for retry_scheduled cases
        # and has no other way to get it without duplicating this function's
        # guardrail-call logic.
        "action_scheduled_for": proposed.get("action_scheduled_for"),
        "decline_code": case_facts["decline_code"],
        "amount": case_facts["amount"],
        "tree_model_score": record["tree_model_score"],
        "routing_rationale": routing_rationale,
        "tree_model_top_features": json.dumps(
            [{"feature": f["feature"], "value": f["value"], "shap_value": f["shap_value"]} for f in shap_top]
        ),
        "routed_to_llm": routed_to_llm,
        "llm_recommended_action": llm_recommended_action,
        "llm_confidence": llm_confidence,
        "llm_reasoning_summary": llm_reasoning_summary,
        "llm_schema_valid": llm_schema_valid,
        "guardrail_flags": ";".join(guardrail_flags),
        "proposed_action": proposed_action,
        "final_action": final_action,
        "guardrail_overrode": guardrail_overrode,
        "override_rule": override_rule,
        "requires_human_review": guardrail_result["requires_human_review"],
        "model_version": model_version,
        "pipeline_version": PIPELINE_VERSION,
        "baseline_final_action": baseline_final_action,
        "baseline_guardrail_overrode": baseline_guardrail_overrode,
        "baseline_override_rule": baseline_override_rule,
        "pipeline_retried": final_action in RECOVERABLE_ACTIONS,
        "baseline_retried": baseline_final_action in RECOVERABLE_ACTIONS,
    }


# --------------------------------------------------------------------------
# Batch orchestration
# --------------------------------------------------------------------------
def run_batch(adapter: "llm_layer.LLMAdapter | None" = None) -> pd.DataFrame:
    """adapter is injectable for tests; defaults to llm_layer.get_llm_adapter()
    (constructed lazily, only if at least one case is routed to the LLM)."""
    meta = shap_extract.load_metadata()
    tree_model_version = meta["models"][meta["primary_model_key"]]["version"]

    scores_df = shap_extract.get_scores_df()
    shap_by_case = shap_extract.run_full_batch()
    routing = confidence_gate.run_full_batch(scores_df)
    band_low = routing["computed_probability_band"]["low"]
    band_high = routing["computed_probability_band"]["high"]

    if adapter is None and any(r["routed_to_llm"] for r in routing["records"]):
        adapter = llm_layer.get_llm_adapter()

    rows = []
    for record in routing["records"]:
        case_id = record["case_id"]
        shap_top = shap_by_case[case_id]
        case_facts = shap_extract.get_case_facts(case_id)
        rows.append(build_audit_row(record, shap_top, case_facts, tree_model_version, adapter, band_low, band_high))

    return pd.DataFrame(rows, columns=AUDIT_COLUMNS)


def write_audit_log(df: pd.DataFrame, path: Path = AUDIT_LOG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


# --------------------------------------------------------------------------
# Batch simulation -- computed from the audit log dataframe, not by
# re-running the pipeline. See module docstring for why the naive baseline
# is guardrailed and why gross vs. net recovered-$ are both reported.
# --------------------------------------------------------------------------
def compute_simulation(audit_df: pd.DataFrame) -> dict:
    ground_truth = shap_extract.load_batch_df()[["case_id", "outcome"]]
    merged = audit_df.merge(ground_truth, on="case_id", how="left")
    if merged["outcome"].isna().any():
        missing = merged.loc[merged["outcome"].isna(), "case_id"].tolist()
        raise ValueError(f"No ground-truth outcome for case_id(s): {missing}")

    would_recover = merged["outcome"] == 1
    pipeline_retried = merged["pipeline_retried"].astype(bool)
    baseline_retried = merged["baseline_retried"].astype(bool)
    pipeline_overrode = merged["guardrail_overrode"].astype(bool)
    baseline_overrode = merged["baseline_guardrail_overrode"].astype(bool)

    pipeline_gross_recovered = float(merged.loc[pipeline_retried & would_recover, "amount"].sum())
    baseline_gross_recovered = float(merged.loc[baseline_retried & would_recover, "amount"].sum())

    pipeline_retry_attempts = int(pipeline_retried.sum())
    baseline_retry_attempts = int(baseline_retried.sum())

    pipeline_net_recovered = pipeline_gross_recovered - pipeline_retry_attempts * COST_PER_RETRY_ATTEMPT
    baseline_net_recovered = baseline_gross_recovered - baseline_retry_attempts * COST_PER_RETRY_ATTEMPT
    net_lift_absolute = pipeline_net_recovered - baseline_net_recovered
    net_lift_pct = (net_lift_absolute / baseline_net_recovered * 100.0) if baseline_net_recovered else float("nan")

    wasted_retries_avoided = baseline_retry_attempts - pipeline_retry_attempts
    # Cases the naive policy would have retried into (recommended_action is
    # always retry_now, a member of RECOVERABLE_ACTIONS) had a guardrail not
    # blocked it -- i.e. every baseline override, since baseline always
    # proposes a retry.
    wasted_retries_on_guaranteed_fails = int(baseline_overrode.sum())

    pipeline_override_counts = merged.loc[pipeline_overrode, "override_rule"].value_counts().to_dict()
    baseline_override_counts = merged.loc[baseline_overrode, "baseline_override_rule"].value_counts().to_dict()

    return {
        "n_cases": len(merged),
        "total_amount": float(merged["amount"].sum()),
        "cost_per_retry_attempt": COST_PER_RETRY_ATTEMPT,
        "pipeline_gross_recovered": pipeline_gross_recovered,
        "baseline_gross_recovered": baseline_gross_recovered,
        "pipeline_retry_attempts": pipeline_retry_attempts,
        "baseline_retry_attempts": baseline_retry_attempts,
        "wasted_retries_avoided": wasted_retries_avoided,
        "wasted_retries_on_guaranteed_fails": wasted_retries_on_guaranteed_fails,
        "pipeline_net_recovered": pipeline_net_recovered,
        "baseline_net_recovered": baseline_net_recovered,
        "net_lift_absolute": net_lift_absolute,
        "net_lift_pct": net_lift_pct,
        "pipeline_override_counts": pipeline_override_counts,
        "baseline_override_counts": baseline_override_counts,
    }


def compute_three_scenario_simulation(audit_df: pd.DataFrame) -> dict:
    """Splits the single pipeline-vs-baseline comparison in compute_simulation
    into three scenarios, because collapsing them into one hid a real
    apples-to-oranges problem: the existing "baseline" is ALREADY guardrailed
    (see module docstring above), but reading its recovered-$ next to the
    pipeline's invited exactly the misreading that a truly UNguardrailed
    "retry everyone, no compliance check" policy would have been fairer to
    the pipeline than it deserves credit for.

    Three scenarios, same audit log, same ground-truth outcomes:

      * naive_no_guardrails -- retries literally every case, zero rule
        checks applied to what counts as "retried". Its compliance_violations
        count reuses the guardrail pass ALREADY run against this exact
        retry_now proposal for the (guardrailed) baseline scenario --
        baseline_guardrail_overrode is True exactly when that retry_now
        proposal would have been blocked by guardrails.py, i.e. exactly this
        policy's compliance violations. This reuses that existing pass-
        through result; it does not re-invoke or alter guardrails.py.
      * compliant_no_targeting -- retries every case the guardrail layer
        allows, with zero tree-model/LLM targeting. This is exactly the
        existing baseline_* audit columns (already guardrailed -- see "Why
        the naive baseline is ALSO guardrailed" above), re-surfaced under a
        clearer name so it sits next to naive_no_guardrails instead of being
        confused with it.
      * pipeline -- the actual ML/LLM-targeted, guardrailed pipeline. Same
        pipeline_* columns compute_simulation already uses.

    Every scenario also reports recovered_rs_per_attempt (gross recovered /
    retry attempts) -- revenue efficiency per retry fired, independent of
    how many cases a policy chose to retry in the first place.
    """
    ground_truth = shap_extract.load_batch_df()[["case_id", "outcome"]]
    merged = audit_df.merge(ground_truth, on="case_id", how="left")
    if merged["outcome"].isna().any():
        missing = merged.loc[merged["outcome"].isna(), "case_id"].tolist()
        raise ValueError(f"No ground-truth outcome for case_id(s): {missing}")

    would_recover = merged["outcome"] == 1
    amount = merged["amount"]
    n_cases = len(merged)
    total_amount = float(amount.sum())

    baseline_retried = merged["baseline_retried"].astype(bool)
    baseline_overrode = merged["baseline_guardrail_overrode"].astype(bool)
    pipeline_retried = merged["pipeline_retried"].astype(bool)
    pipeline_overrode = merged["guardrail_overrode"].astype(bool)

    def _scenario(gross: float, attempts: int, compliance_violations: int) -> dict:
        net = gross - attempts * COST_PER_RETRY_ATTEMPT
        return {
            "retry_attempts": attempts,
            "gross_recovered": gross,
            "net_recovered": net,
            "net_recovered_pct": (net / total_amount * 100.0) if total_amount else float("nan"),
            "recovered_rs_per_attempt": (gross / attempts) if attempts else 0.0,
            "compliance_violations": compliance_violations,
        }

    naive_no_guardrails = _scenario(
        gross=float(amount[would_recover].sum()),
        attempts=n_cases,
        compliance_violations=int(baseline_overrode.sum()),
    )
    compliant_no_targeting = _scenario(
        gross=float(amount[baseline_retried & would_recover].sum()),
        attempts=int(baseline_retried.sum()),
        # Guaranteed 0 by construction (an overridden proposal is forced to
        # NO_RETRY_ACTION, so it can never also count as retried) -- computed
        # rather than hardcoded, as a live check of that invariant.
        compliance_violations=int((baseline_retried & baseline_overrode).sum()),
    )
    pipeline = _scenario(
        gross=float(amount[pipeline_retried & would_recover].sum()),
        attempts=int(pipeline_retried.sum()),
        compliance_violations=int((pipeline_retried & pipeline_overrode).sum()),
    )

    def _lift(a: dict, b: dict) -> dict:
        absolute = a["net_recovered"] - b["net_recovered"]
        pct = (absolute / b["net_recovered"] * 100.0) if b["net_recovered"] else float("nan")
        return {"absolute": absolute, "pct": pct}

    return {
        "n_cases": n_cases,
        "total_amount": total_amount,
        "cost_per_retry_attempt": COST_PER_RETRY_ATTEMPT,
        "naive_no_guardrails": naive_no_guardrails,
        "compliant_no_targeting": compliant_no_targeting,
        "pipeline": pipeline,
        "lift_pipeline_vs_naive_no_guardrails": _lift(pipeline, naive_no_guardrails),
        "lift_pipeline_vs_compliant_no_targeting": _lift(pipeline, compliant_no_targeting),
    }


def compute_compliance_check(audit_df: pd.DataFrame) -> dict:
    """Verifies the "zero uncompliant executions" claim used in the pitch-
    numbers summary, rather than just asserting final_action is a
    well-formed enum value (see validate_audit_log.check_final_action_enum,
    which checks the value is valid but not that it's compliant). No case
    should execute a retry (retry_now/retry_scheduled) despite a
    hard-decline or NPCI/network retry-cap guardrail flag having fired on
    it -- guardrails.apply_guardrails should always force those to
    no_retry_prompt_update, so this is a sanity check on that invariant
    actually holding in the audit log, not a new rule."""
    non_compliant = audit_df[
        audit_df["final_action"].isin(["retry_now", "retry_scheduled"])
        & (
            audit_df["decline_code"].isin(guardrails.HARD_DECLINE_CODES)
            | audit_df["guardrail_flags"].str.contains(
                "npci_retry_cap_reached|network_retry_cap_exceeded", na=False
            )
        )
    ]
    return {
        "n_cases": len(audit_df),
        "non_compliant_count": len(non_compliant),
        "non_compliant_case_ids": non_compliant["case_id"].tolist(),
    }


def _lift_phrase(net_lift_absolute: float, net_lift_pct: float, prior_net_lift_pct: "float | None" = None) -> str:
    """Sign-aware prose for net_lift_absolute/net_lift_pct -- a negative
    lift is a shortfall, not an "improvement" (see PHASE7_REPORT.md's
    real-Gemini-run update for the bug this replaces: the old summary
    sentence hardcoded the word "improvement" regardless of sign, which
    read as self-contradicting -- or as spin -- whenever the pipeline was
    behind the naive baseline on net recovered-$)."""
    if net_lift_absolute >= 0:
        return f"a Rs {net_lift_absolute:,.2f} ({net_lift_pct:.1f}%) net improvement"

    phrase = (
        f"a Rs {abs(net_lift_absolute):,.2f} ({abs(net_lift_pct):.1f}%) "
        f"net shortfall versus the naive baseline"
    )
    if prior_net_lift_pct is not None:
        phrase += f", narrowed from a {abs(prior_net_lift_pct):.1f}% shortfall on the prior run"
    return phrase


def write_pitch_numbers(
    sim: dict,
    compliance: dict,
    path: Path = PITCH_NUMBERS_PATH,
    prior_net_lift_pct: "float | None" = None,
) -> None:
    """prior_net_lift_pct: this repo has no run-history log yet, so there is
    no prior-run figure to pull automatically -- pass it explicitly if the
    caller has one (e.g. from a previous report), otherwise the shortfall
    is stated plainly with no narrowing comparison, per instruction not to
    fabricate a number."""
    total = sim["total_amount"]
    pipeline_gross_pct = (sim["pipeline_gross_recovered"] / total * 100.0) if total else 0.0
    baseline_gross_pct = (sim["baseline_gross_recovered"] / total * 100.0) if total else 0.0
    lift_sign = "improvement" if sim["net_lift_absolute"] >= 0 else "shortfall"
    summary_lift_phrase = _lift_phrase(sim["net_lift_absolute"], sim["net_lift_pct"], prior_net_lift_pct)

    if compliance["non_compliant_count"] == 0:
        compliance_line = f"Compliance check: 0/{compliance['n_cases']} cases executed a retry against a guardrail-blocked case (verified)."
        compliance_phrase = f"zero uncompliant executions ({compliance['n_cases']} cases verified)"
    else:
        compliance_line = (
            f"Compliance check: {compliance['non_compliant_count']}/{compliance['n_cases']} cases executed a "
            f"retry against a guardrail-blocked case (verified) -- case_id(s): {compliance['non_compliant_case_ids']}"
        )
        compliance_phrase = (
            f"{compliance['non_compliant_count']} uncompliant execution(s) out of {compliance['n_cases']} "
            f"cases verified -- see Guardrail Overrides section"
        )

    def _override_lines(counts: dict) -> str:
        lines = "\n".join(f"- {rule}: {count}" for rule, count in counts.items())
        return lines or "- (none)"

    content = f"""# Pitch Numbers -- Batch Simulation

Generated: {datetime.now(timezone.utc).isoformat()}

## a. Batch

- Cases in batch: {sim['n_cases']}
- Total amount at stake: Rs {sim['total_amount']:,.2f}

## b. Gross Recovered

- Pipeline: Rs {sim['pipeline_gross_recovered']:,.2f} ({pipeline_gross_pct:.1f}% of total)
- Naive baseline (retry every case once, guardrailed): Rs {sim['baseline_gross_recovered']:,.2f} ({baseline_gross_pct:.1f}% of total)

Note: the pipeline's gross figure may be lower than or comparable to the naive baseline's -- it retries fewer, more carefully selected cases, so it is expected to leave some recoverable revenue on the table in exchange for far fewer wasted attempts. Gross recovered-$ is reported here for transparency but is not the headline number (see Net Recovered below).

## c. Retry Attempts

- Pipeline retry attempts: {sim['pipeline_retry_attempts']}
- Naive baseline retry attempts: {sim['baseline_retry_attempts']}
- Wasted retries avoided (baseline - pipeline): {sim['wasted_retries_avoided']}
- Of the baseline's attempts, blocked into guaranteed-fail cases by compliance guardrails (hard declines, NPCI/network retry caps) had it not been guardrailed: {sim['wasted_retries_on_guaranteed_fails']}

## d. Net Recovered (headline)

Assumes Rs {sim['cost_per_retry_attempt']:,.2f} cost per retry attempt executed (see COST_PER_RETRY_ATTEMPT in pipeline/run_batch.py -- a placeholder assumption, not derived from data; tune as real per-attempt cost figures become available).

- Pipeline net recovered: Rs {sim['pipeline_net_recovered']:,.2f}
- Naive baseline net recovered: Rs {sim['baseline_net_recovered']:,.2f}
- Net lift (absolute): Rs {sim['net_lift_absolute']:,.2f} ({lift_sign})
- Net lift (%): {sim['net_lift_pct']:.1f}% ({lift_sign})

## e. Guardrail Overrides

Pipeline overrides, by rule:
{_override_lines(sim['pipeline_override_counts'])}

Naive baseline overrides, by rule (what a blind "retry everyone" policy would have executed into, had guardrails not blocked it):
{_override_lines(sim['baseline_override_counts'])}

{compliance_line}

## f. Summary

Naive retry-everything recovers Rs {sim['baseline_gross_recovered']:,.2f} gross using {sim['baseline_retry_attempts']} retry attempts, including {sim['wasted_retries_on_guaranteed_fails']} against guaranteed-fail cases blocked by compliance guardrails. Our pipeline recovers Rs {sim['pipeline_gross_recovered']:,.2f} gross using only {sim['pipeline_retry_attempts']} attempts (a {sim['wasted_retries_avoided']}-attempt reduction) -- net of an assumed Rs {sim['cost_per_retry_attempt']:,.2f}/attempt cost, that's Rs {sim['pipeline_net_recovered']:,.2f} vs Rs {sim['baseline_net_recovered']:,.2f}, {summary_lift_phrase}, with {compliance_phrase}.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> None:
    start = time.monotonic()
    audit_df = run_batch()
    write_audit_log(audit_df)
    sim = compute_simulation(audit_df)
    compliance = compute_compliance_check(audit_df)
    write_pitch_numbers(sim, compliance)
    elapsed = time.monotonic() - start

    total = len(audit_df)
    routed = int(audit_df["routed_to_llm"].sum())
    routed_pct = 100.0 * routed / total if total else 0.0
    overrides = int(audit_df["guardrail_overrode"].sum())

    print(f"Phase 7: wrote {total} rows to {AUDIT_LOG_PATH}")
    print(f"Routed to LLM: {routed}/{total} ({routed_pct:.1f}%)")
    print(f"Pipeline guardrail overrides: {overrides}")
    print()
    print(f"{'':28s} {'Pipeline':>16s} {'Naive baseline':>16s}")
    print(f"{'Retry attempts':28s} {sim['pipeline_retry_attempts']:>16d} {sim['baseline_retry_attempts']:>16d}")
    print(
        f"{'Gross recovered (Rs)':28s} "
        f"{sim['pipeline_gross_recovered']:>16,.2f} {sim['baseline_gross_recovered']:>16,.2f}"
    )
    print(
        f"{'Net recovered (Rs)':28s} "
        f"{sim['pipeline_net_recovered']:>16,.2f} {sim['baseline_net_recovered']:>16,.2f}"
    )
    print()
    print(f"Wasted retries avoided: {sim['wasted_retries_avoided']}")
    print(f"Wasted retries on guaranteed-fail cases (baseline, pre-guardrail): {sim['wasted_retries_on_guaranteed_fails']}")
    lift_sign = "improvement" if sim["net_lift_absolute"] >= 0 else "shortfall"
    print(f"Net lift: Rs {sim['net_lift_absolute']:,.2f} ({sim['net_lift_pct']:.1f}%) [{lift_sign}]")
    print(f"Compliance check: {compliance['non_compliant_count']}/{compliance['n_cases']} non-compliant executions")
    print(f"Wrote pitch numbers to {PITCH_NUMBERS_PATH}")
    print(f"Elapsed: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
