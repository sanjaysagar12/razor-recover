"""
Phase 8 -- Showcase case selection.

Reads logs/audit_log.csv (written by pipeline/run_batch.py -- Phase 8 does
not re-run the pipeline or touch pipeline/ logic, it only consumes that
output) and picks 4 case_ids that best demonstrate the four distinct paths
a case can take through the pipeline:

  1. tree_only           -- never routed to the LLM (tree-model template
                             path only), no guardrail override, no human
                             review -- the "boring, clean" majority path.
  2. llm_legible          -- LLM was invoked and produced a reasoning_summary
                             that is schema-valid, not flagged as possibly
                             ungrounded, and not tangled up with a guardrail
                             override or human-review escalation, so the
                             SHAP-grounded reasoning is legible on its own.
  3. guardrail_override   -- the guardrail layer overrode the proposed
                             action. Preference order: npci_retry_cap_reached
                             or hard_decline_excluded (the two rules the
                             user asked for specifically), picking the
                             cleanest single-flag example.
  4. human_review         -- final requires_human_review == True, picked so
                             it does NOT overlap with the guardrail-override
                             pick (a case escalated by the LLM's own
                             judgement, not by a guardrail rule firing).

Each pick is selected deterministically (lowest case_id satisfying the
selection tier) so re-running this script against an unchanged audit log
always reproduces the same 4 case_ids. If a category has no candidate at
all, this script prints an explicit error and exits non-zero rather than
silently falling back to a weaker example.

Run: python demo/select_showcase_cases.py
Writes: demo/showcase_cases.txt (label=case_id, one per line)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
AUDIT_LOG_PATH = BASE_DIR / "logs" / "audit_log.csv"
OUTPUT_PATH = Path(__file__).resolve().parent / "showcase_cases.txt"

MIN_LEGIBLE_REASONING_CHARS = 200

# Preference order for the guardrail-override pick, per the user's request
# ("ideally a hard-decline or NPCI-cap override").
PREFERRED_OVERRIDE_RULES = ["hard_decline_excluded", "npci_retry_cap_reached"]


def _coerce_bool(series: pd.Series) -> pd.Series:
    if series.dtype == bool:
        return series
    return series.astype(str).str.strip().str.lower().eq("true")


def load_audit_log() -> pd.DataFrame:
    if not AUDIT_LOG_PATH.exists():
        print(f"ERROR: {AUDIT_LOG_PATH} does not exist -- run pipeline/run_batch.py first.")
        sys.exit(1)
    df = pd.read_csv(AUDIT_LOG_PATH)
    for col in ("routed_to_llm", "guardrail_overrode", "requires_human_review"):
        df[col] = _coerce_bool(df[col])
    return df.sort_values("case_id", kind="stable").reset_index(drop=True)


# --------------------------------------------------------------------------
# Pick 1: tree-only path
# --------------------------------------------------------------------------
def pick_tree_only(df: pd.DataFrame, exclude: set[str]) -> tuple[str, str]:
    candidates = df[
        (~df["routed_to_llm"])
        & (~df["guardrail_overrode"])
        & (~df["requires_human_review"])
        & (~df["case_id"].isin(exclude))
    ]
    if candidates.empty:
        raise RuntimeError(
            "No case found that (a) was never routed to the LLM, (b) had no guardrail "
            "override, and (c) does not require human review -- cannot pick a clean "
            "tree-only showcase case from this batch."
        )
    case_id = candidates.iloc[0]["case_id"]
    reason = "routed_to_llm=False, guardrail_overrode=False, requires_human_review=False (clean tree-template path)"
    return case_id, reason


# --------------------------------------------------------------------------
# Pick 2: LLM invoked, legible SHAP-grounded reasoning
# --------------------------------------------------------------------------
def pick_llm_legible(df: pd.DataFrame, exclude: set[str]) -> tuple[str, str]:
    base = df[
        (df["routed_to_llm"])
        & (df["llm_schema_valid"] == True)  # noqa: E712
        & (~df["case_id"].isin(exclude))
    ]

    def _clean(sub: pd.DataFrame) -> pd.DataFrame:
        no_flags = sub["guardrail_flags"].isna() | (sub["guardrail_flags"].astype(str).str.strip() == "")
        long_enough = sub["llm_reasoning_summary"].astype(str).str.len() >= MIN_LEGIBLE_REASONING_CHARS
        return sub[no_flags & long_enough & (~sub["requires_human_review"]) & (~sub["guardrail_overrode"])]

    tier1 = _clean(base)
    if not tier1.empty:
        case_id = tier1.iloc[0]["case_id"]
        return case_id, (
            "routed_to_llm=True, llm_schema_valid=True, no guardrail_flags, "
            "requires_human_review=False, reasoning_summary "
            f">= {MIN_LEGIBLE_REASONING_CHARS} chars (clean, standalone-legible LLM path)"
        )

    # Relaxed fallback: still schema-valid and long enough, but may carry a
    # guardrail flag or review flag -- still legible, just not as clean.
    long_enough = base["llm_reasoning_summary"].astype(str).str.len() >= MIN_LEGIBLE_REASONING_CHARS
    tier2 = base[long_enough]
    if not tier2.empty:
        case_id = tier2.iloc[0]["case_id"]
        return case_id, (
            f"routed_to_llm=True, llm_schema_valid=True, reasoning_summary >= "
            f"{MIN_LEGIBLE_REASONING_CHARS} chars (relaxed: no clean case without "
            "an overlapping guardrail/review flag was found)"
        )

    raise RuntimeError(
        "No case found where the LLM was invoked, produced a schema-valid decision, and "
        f"wrote a reasoning_summary of at least {MIN_LEGIBLE_REASONING_CHARS} chars -- cannot "
        "pick a legible LLM-reasoning showcase case from this batch."
    )


# --------------------------------------------------------------------------
# Pick 3: guardrail override
# --------------------------------------------------------------------------
def pick_guardrail_override(df: pd.DataFrame, exclude: set[str]) -> tuple[str, str]:
    overridden = df[(df["guardrail_overrode"]) & (~df["case_id"].isin(exclude))]
    if overridden.empty:
        raise RuntimeError(
            "No case in this audit log has guardrail_overrode=True -- the guardrail layer "
            "never overrode a proposed action on this batch, so no guardrail-override "
            "showcase case can be picked. This should be reported, not silently skipped."
        )

    override_rule_counts = overridden["override_rule"].value_counts().to_dict()

    for preferred_rule in PREFERRED_OVERRIDE_RULES:
        rule_rows = overridden[overridden["override_rule"] == preferred_rule]
        if rule_rows.empty:
            continue
        # Cleanest: single flag (just this rule), no compounding human-review escalation.
        single_flag = rule_rows[
            (rule_rows["guardrail_flags"] == preferred_rule) & (~rule_rows["requires_human_review"])
        ]
        pick_pool = single_flag if not single_flag.empty else rule_rows
        case_id = pick_pool.iloc[0]["case_id"]
        clean_note = "single-flag, requires_human_review=False" if not single_flag.empty else "compounded with other flags"
        return case_id, (
            f"guardrail_overrode=True, override_rule={preferred_rule!r} ({clean_note}). "
            f"All override_rule counts in this batch: {override_rule_counts}"
        )

    # Neither preferred rule fired as an override -- fall back to whatever did,
    # but say so explicitly rather than pretending it was the preferred kind.
    case_id = overridden.iloc[0]["case_id"]
    return case_id, (
        f"NEITHER {PREFERRED_OVERRIDE_RULES} fired as an override in this batch -- falling back to "
        f"override_rule={overridden.iloc[0]['override_rule']!r}. All override_rule counts: {override_rule_counts}"
    )


# --------------------------------------------------------------------------
# Pick 4: requires_human_review
# --------------------------------------------------------------------------
def pick_human_review(df: pd.DataFrame, exclude: set[str]) -> tuple[str, str]:
    base = df[(df["requires_human_review"]) & (~df["case_id"].isin(exclude))]
    if base.empty:
        raise RuntimeError(
            "No case has requires_human_review=True (excluding cases already used for other "
            "showcase slots) -- cannot pick a human-review showcase case from this batch."
        )
    # Prefer a case where the escalation came from the LLM's own judgement,
    # not from a guardrail override -- so this pick tells a distinct story
    # from pick 3.
    clean = base[~base["guardrail_overrode"]]
    pick_pool = clean if not clean.empty else base
    case_id = pick_pool.iloc[0]["case_id"]
    note = "guardrail_overrode=False (escalation driven by the LLM/tree path itself)" if not clean.empty else (
        "every requires_human_review=True case in this batch also had a guardrail override -- "
        "picking the lowest case_id anyway"
    )
    return case_id, f"requires_human_review=True, {note}"


def main() -> int:
    df = load_audit_log()
    print(f"Loaded {len(df)} rows from {AUDIT_LOG_PATH}\n")

    picks: list[tuple[str, str, str]] = []  # (label, case_id, reason)
    exclude: set[str] = set()

    try:
        for label, pick_fn in [
            ("tree_only", pick_tree_only),
            ("llm_legible", pick_llm_legible),
            ("guardrail_override", pick_guardrail_override),
            ("human_review", pick_human_review),
        ]:
            case_id, reason = pick_fn(df, exclude)
            picks.append((label, case_id, reason))
            exclude.add(case_id)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"{'Label':<20s} {'case_id':<12s} Reason")
    print("-" * 100)
    for label, case_id, reason in picks:
        print(f"{label:<20s} {case_id:<12s} {reason}")
    print()

    OUTPUT_PATH.write_text(
        "\n".join(f"{label}={case_id}" for label, case_id, _ in picks) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(picks)} showcase case_ids to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
