"""
Phase 4 -- Orchestrator.

Wires shap_extract.py (SHAP explanations from the Phase 3 primary model) and
confidence_gate.py (probability-band / ambiguous-code routing) together into
one combined record per case, and writes the result to logs/phase4_output.json.

Run with:
    python pipeline/run_phase4.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import confidence_gate
import shap_extract

BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
OUTPUT_PATH = LOGS_DIR / "phase4_output.json"


def build_combined_output() -> dict:
    scores_df = shap_extract.get_scores_df()
    shap_by_case = shap_extract.run_full_batch()
    routing_result = confidence_gate.run_full_batch(scores_df)

    combined = []
    for rec in routing_result["records"]:
        case_id = rec["case_id"]
        combined.append(
            {
                "case_id": case_id,
                "tree_model_score": rec["tree_model_score"],
                "shap_top_features": shap_by_case[case_id],
                "routed_to_llm": rec["routed_to_llm"],
                "routing_trigger": rec["routing_trigger"],
                "template_action": rec["template_action"],
            }
        )
    return {
        "computed_probability_band": routing_result["computed_probability_band"],
        "records": combined,
    }


def write_output(output: dict, path: Path = OUTPUT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)


def main() -> list[dict]:
    """Returns just the records (the shape tests/other callers care about);
    the computed band is available via build_combined_output()."""
    return build_combined_output()["records"]


if __name__ == "__main__":
    output = build_combined_output()
    write_output(output)

    records = output["records"]
    band = output["computed_probability_band"]
    total = len(records)
    routed = sum(1 for r in records if r["routed_to_llm"])
    routed_pct = 100.0 * routed / total if total else 0.0
    band_only = sum(1 for r in records if r["routing_trigger"] == ["probability_band"])
    code_only = sum(1 for r in records if r["routing_trigger"] == ["ambiguous_code"])
    both = sum(1 for r in records if set(r["routing_trigger"]) == {"probability_band", "ambiguous_code"})

    print(f"Phase 4: wrote {total} records to {OUTPUT_PATH}")
    print(f"Computed probability band: low={band['low']:.4f}, high={band['high']:.4f}")
    print(f"Routed to LLM: {routed}/{total} ({routed_pct:.1f}%)")
    print(f"  probability_band only: {band_only}")
    print(f"  ambiguous_code only:   {code_only}")
    print(f"  both triggers:         {both}")
