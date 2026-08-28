"""
One-time backfill: adds the "routing_rationale" column (see run_batch.py's
_routing_rationale, added after the original Phase 8 demo) to an
ALREADY-WRITTEN logs/audit_log.csv, without re-running the LLM layer or
guardrails, and without changing any existing cell.

Why this is safe to do without a full pipeline/run_batch.py re-run: the
confidence-gate routing decision (tree_model_score, the probability band,
routed_to_llm, routing_trigger) is a deterministic function of the trained
model + this batch's data only -- it does not depend on the LLM at all.
Recomputing it via shap_extract.get_scores_df() + confidence_gate.run_full_batch()
reproduces EXACTLY what run_batch.py already computed when it wrote the
existing audit log, so this script cross-checks that recomputation against
the audit log's own routed_to_llm/tree_model_score columns before writing
anything, and aborts loudly on any mismatch instead of silently overwriting.

Run: python pipeline/backfill_routing_rationale.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

import confidence_gate
import run_batch
import shap_extract

BASE_DIR = Path(__file__).resolve().parent.parent
AUDIT_LOG_PATH = BASE_DIR / "logs" / "audit_log.csv"

SCORE_TOLERANCE = 1e-6


def main() -> int:
    if not AUDIT_LOG_PATH.exists():
        print(f"ERROR: {AUDIT_LOG_PATH} does not exist -- run pipeline/run_batch.py first.")
        return 1

    audit_df = pd.read_csv(AUDIT_LOG_PATH)
    if "routing_rationale" in audit_df.columns:
        print(f"{AUDIT_LOG_PATH} already has a routing_rationale column -- nothing to do.")
        return 0

    audit_df["routed_to_llm"] = audit_df["routed_to_llm"].astype(str).str.strip().str.lower().eq("true")

    print("Recomputing confidence-gate routing (deterministic, no LLM/network calls) ...")
    scores_df = shap_extract.get_scores_df()
    routing = confidence_gate.run_full_batch(scores_df)
    band_low = routing["computed_probability_band"]["low"]
    band_high = routing["computed_probability_band"]["high"]
    records_by_case = {r["case_id"]: r for r in routing["records"]}

    print(f"Recomputed band: [{band_low:.6f}, {band_high:.6f}]")

    rationales = []
    mismatches = []
    for row in audit_df.itertuples(index=False):
        record = records_by_case.get(row.case_id)
        if record is None:
            mismatches.append(f"{row.case_id}: not found in recomputed scores_df")
            rationales.append(None)
            continue

        if record["routed_to_llm"] != row.routed_to_llm:
            mismatches.append(
                f"{row.case_id}: routed_to_llm mismatch -- audit log={row.routed_to_llm}, "
                f"recomputed={record['routed_to_llm']}"
            )
        score_diff = abs(record["tree_model_score"] - row.tree_model_score)
        if score_diff > SCORE_TOLERANCE:
            mismatches.append(
                f"{row.case_id}: tree_model_score mismatch -- audit log={row.tree_model_score}, "
                f"recomputed={record['tree_model_score']} (diff={score_diff:.2e})"
            )

        rationales.append(
            run_batch._routing_rationale(
                record["tree_model_score"],
                row.decline_code,
                band_low,
                band_high,
                record["routing_trigger"],
                record["routed_to_llm"],
                record["template_action"],
            )
        )

    if mismatches:
        print(f"ABORTING -- {len(mismatches)} mismatch(es) between the audit log and recomputed routing:")
        for m in mismatches[:20]:
            print(f"  {m}")
        if len(mismatches) > 20:
            print(f"  ... and {len(mismatches) - 20} more")
        print("No file was written.")
        return 1

    print(f"Cross-check passed: 0 mismatches across {len(audit_df)} rows.")

    audit_df.insert(audit_df.columns.get_loc("tree_model_score") + 1, "routing_rationale", rationales)
    audit_df.to_csv(AUDIT_LOG_PATH, index=False)
    print(f"Wrote routing_rationale column to {AUDIT_LOG_PATH} ({len(audit_df)} rows, all other columns unchanged).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
