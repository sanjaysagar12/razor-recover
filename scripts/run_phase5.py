"""
Phase 5 -- Orchestrator.

Reads Phase 4's output (logs/phase4_output.json). For every case with
routed_to_llm == true, assembles a case payload (tree score + SHAP top-5 +
raw case facts) and runs it through the LLM layer (llm_layer.py) via
whichever provider LLM_PROVIDER selects. Cases with routed_to_llm == false
pass through untouched, carrying their Phase 4 template_action forward as
the final action -- they never call the LLM.

This is the "run_batch.py" batch orchestrator from the Phase 5 spec, named
run_phase5.py to match this repo's run_phaseN.py convention (see
run_phase4.py).

Run with:
    python scripts/run_phase5.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import llm_layer, shap_extract

BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
PHASE4_OUTPUT_PATH = LOGS_DIR / "phase4_output.json"
OUTPUT_PATH = LOGS_DIR / "phase5_output.json"


def _build_case_payload(record: dict) -> dict:
    case_id = record["case_id"]
    return {
        "case_id": case_id,
        "tree_model_score": record["tree_model_score"],
        "shap_top_features": record["shap_top_features"],
        "case_facts": shap_extract.get_case_facts(case_id),
    }


def run_batch(phase4_records: list[dict], adapter: "llm_layer.LLMAdapter | None" = None) -> list[dict]:
    """adapter is injectable for tests; defaults to get_llm_adapter()."""
    adapter = adapter or llm_layer.get_llm_adapter()

    results = []
    for record in phase4_records:
        if not record["routed_to_llm"]:
            results.append(
                {
                    "case_id": record["case_id"],
                    "routed_to_llm": False,
                    "final_action": record["template_action"],
                    "llm_output": None,
                }
            )
            continue

        case = _build_case_payload(record)
        llm_output = llm_layer.get_llm_decision(adapter, case)
        results.append(
            {
                "case_id": record["case_id"],
                "routed_to_llm": True,
                "final_action": llm_output["recommended_action"],
                "llm_output": llm_output,
            }
        )
    return results


def main() -> list[dict]:
    with open(PHASE4_OUTPUT_PATH, "r", encoding="utf-8") as f:
        phase4_output = json.load(f)

    return run_batch(phase4_output["records"])


if __name__ == "__main__":
    start = time.monotonic()
    results = main()
    elapsed = time.monotonic() - start

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    total = len(results)
    routed = sum(1 for r in results if r["routed_to_llm"])
    escalated = sum(1 for r in results if r["routed_to_llm"] and r["llm_output"]["requires_human_review"])

    print(f"Phase 5: wrote {total} records to {OUTPUT_PATH}")
    print(f"Routed to LLM: {routed}/{total}")
    print(f"  requires_human_review: {escalated}/{routed if routed else 0}")
    print(f"Elapsed: {elapsed:.1f}s")
