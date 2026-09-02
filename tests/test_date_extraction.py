"""
Promise-to-pay date extraction tests -- pipeline.llm_layer.extract_promise_date.

Live-calls the configured LLM_PROVIDER (matches tests/test_llm_layer.py's
convention: real call when a usable key is present, informational-only
skip note otherwise) against the ten manual-inspection cases from the task
spec, and asserts the no-crash / schema-shape contract that must hold
regardless of what a given model happens to say about any one phrase.

Run with:
    python -m pytest tests/test_date_extraction.py -v -s
    python tests/test_date_extraction.py          # prints full results, no pytest needed
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from pipeline import llm_layer

IST = timezone(timedelta(hours=5, minutes=30))
TODAY = datetime.now(IST).date().isoformat()

CASE_CONTEXT = {"today": TODAY, "case_id": "case_test_promise_0001", "amount": 662.42, "decline_code": "05_do_not_honor"}

# (label, message) -- see task spec for the expectation each one is checking.
MANUAL_CASES = [
    ("specific day-of-month", "I'll pay on the 5th"),
    ("relative -- a week", "give me a week"),
    ("no commitment -- inability to pay", "I'm broke right now"),
    ("vague -- too unspecific", "maybe next month"),
    ("relative -- named weekday", "next Friday works for me"),
    ("relative -- end of month", "end of the month"),
    ("vague -- bare 'soon'", "soon"),
    ("specific full date", "pay you back on 15 sep 2026"),
    ("empty string", ""),
    ("refusal / hostile, no date", "definitely not paying, this is a scam"),
]


def _has_real_key(env_var: str) -> bool:
    import os

    value = os.environ.get(env_var, "")
    return bool(value) and "your_" not in value


def _get_adapter():
    """One adapter instance, reused across all ten cases (extract_promise_date
    accepts `adapter` for exactly this) -- matches whichever LLM_PROVIDER is
    configured, same provider selection get_llm_decision uses elsewhere."""
    import os

    provider = os.environ.get("LLM_PROVIDER", "claude").strip().lower()
    key_var = "ANTHROPIC_API_KEY" if provider == "claude" else "GEMINI_API_KEY"
    if not _has_real_key(key_var):
        pytest.skip(f"{key_var} not configured -- skipping live date-extraction call")
    return llm_layer.get_llm_adapter()


def _run_all_cases(adapter) -> list[dict]:
    results = []
    for label, message in MANUAL_CASES:
        result = llm_layer.extract_promise_date(message, CASE_CONTEXT, adapter=adapter)
        results.append({"label": label, "message": message, **result})
    return results


def test_date_extraction_manual_cases_no_crash_and_schema_valid():
    """The ten task-spec cases, run for real: every one must come back
    schema-shaped (no unhandled exception, no llm_validation_error leaking
    through) regardless of what date the model actually resolves."""
    from pipeline import schema

    adapter = _get_adapter()
    results = _run_all_cases(adapter)

    print(f"\n\ntoday (IST) = {TODAY}\n")
    for r in results:
        print(json.dumps(r, indent=2, default=str))
        print("-" * 60)

    for r in results:
        assert "llm_validation_error" not in r, (r["label"], r.get("llm_validation_error"))
        schema.PromiseDateExtraction.model_validate(
            {k: r[k] for k in ("extracted_date", "confidence", "ambiguous", "clarification_needed")}
        )
        assert 0.0 <= r["confidence"] <= 1.0
        if r["ambiguous"]:
            assert r["extracted_date"] is None, r["label"]
            assert r["clarification_needed"], r["label"]
        else:
            assert r["extracted_date"] is not None, r["label"]


def _fallback_mock_adapter():
    """Used only by the __main__ block when no real key is configured, so
    the script still runs end-to-end (with an obviously-labeled mock) rather
    than doing nothing."""

    class _AlwaysAmbiguousAdapter(llm_layer.LLMAdapter):
        def generate(self, case: dict) -> dict:  # pragma: no cover -- unused here
            raise NotImplementedError

        def extract_date(self, message: str, case_context: dict) -> dict:
            return {
                **llm_layer._echoed_date_fields("mock:no-key-configured"),
                "extracted_date": None,
                "confidence": 0.0,
                "ambiguous": True,
                "clarification_needed": "Could you give me a specific date you'd like to pay by?",
            }

    return _AlwaysAmbiguousAdapter()


def main() -> None:
    import os

    provider = os.environ.get("LLM_PROVIDER", "claude").strip().lower()
    key_var = "ANTHROPIC_API_KEY" if provider == "claude" else "GEMINI_API_KEY"
    if _has_real_key(key_var):
        adapter = llm_layer.get_llm_adapter()
        print(f"Provider: {provider} ({type(adapter).__name__})")
    else:
        adapter = _fallback_mock_adapter()
        print(f"No usable {key_var} configured -- using a mock adapter (all results will be ambiguous).")

    print(f"today (IST) = {TODAY}\n")
    for label, message in MANUAL_CASES:
        result = llm_layer.extract_promise_date(message, CASE_CONTEXT, adapter=adapter)
        print(f"[{label}] message={message!r}")
        print(json.dumps(result, indent=2, default=str))
        print("-" * 60)


if __name__ == "__main__":
    main()
