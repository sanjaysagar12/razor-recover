"""
Phase 5 tests -- LLM layer.

Live-calls a provider when its API key is set in the environment (real
schema-compliance verification against the actual SDK); falls back to a
mocked adapter otherwise, so the suite never hard-fails just because a key
isn't configured locally.

Run with:
    python -m pytest pipeline/test_llm_layer.py -v
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm_layer
import schema

SAMPLE_CASE = {
    "case_id": "case_test_0001",
    "tree_model_score": 0.44,
    "shap_top_features": [
        {"feature": "historical_ptp_honor_rate", "value": 0.69, "shap_value": 0.29},
        {"feature": "ltv_tier", "value": "low", "shap_value": -0.20},
        {"feature": "payment_rail", "value": "emandate", "shap_value": -0.15},
    ],
    "case_facts": {
        "case_id": "case_test_0001",
        "decline_code": "issuer_unavailable",
        "guardrail_flags": None,
        "retry_attempt_number": 1,
        "hours_since_last_attempt": 56.0,
        "customer_tenure_days": 358,
        "historical_ptp_honor_rate": 0.69,
        "amount": 662.42,
        "amount_vs_historical_avg": 0.85,
        "prior_retry_success_count": 0,
        "is_peak_execution_window": False,
        "decline_code_bucket": "AMBIGUOUS",
        "ltv_tier": "low",
        "time_of_day_bucket": "evening",
        "day_of_week": "sat",
        "issuer_bank_risk_tier": "low_risk",
        "payment_rail": "emandate",
    },
}


class _MockAdapter(llm_layer.LLMAdapter):
    """Stand-in for a real provider adapter when no API key is configured."""

    def generate(self, case: dict) -> dict:
        return {
            **llm_layer._echoed_fields(case, "mock:test-model"),
            "recommended_action": "retry_scheduled",
            "action_scheduled_for": "2026-08-27T09:00:00+00:00",
            "confidence": 0.72,
            "reasoning_summary": (
                "historical_ptp_honor_rate is high and ltv_tier/payment_rail pull the "
                "score down only slightly -- worth one more scheduled retry."
            ),
            "guardrail_flags": [],
            "requires_human_review": False,
        }


def _has_real_key(env_var: str) -> bool:
    """True only for a value that looks like an actual credential -- .env
    ships with placeholder text (e.g. "your_anthropic_api_key_here") in this
    repo, which is truthy but not usable, so a plain existence check would
    silently try (and fail) a live call instead of falling back to the mock."""
    value = os.environ.get(env_var, "")
    return bool(value) and "your_" not in value


def _claude_adapter():
    return llm_layer.ClaudeAdapter() if _has_real_key("ANTHROPIC_API_KEY") else _MockAdapter()


def _gemini_adapter():
    return llm_layer.GeminiAdapter() if _has_real_key("GEMINI_API_KEY") else _MockAdapter()


def _assert_schema_compliant(result: dict) -> None:
    assert "llm_validation_error" not in result, result.get("llm_validation_error")
    assert result["recommended_action"] in schema.RECOMMENDED_ACTIONS
    assert 0.0 <= result["confidence"] <= 1.0
    assert isinstance(result["reasoning_summary"], str) and result["reasoning_summary"]
    assert result["case_id"] == SAMPLE_CASE["case_id"]
    # Required by schema.LLMDecision -- re-validate to be sure nothing slipped through.
    schema.LLMDecision.model_validate(result)


def test_claude_adapter_produces_schema_compliant_output():
    result = llm_layer.get_llm_decision(_claude_adapter(), SAMPLE_CASE)
    _assert_schema_compliant(result)


def test_gemini_adapter_produces_schema_compliant_output():
    result = llm_layer.get_llm_decision(_gemini_adapter(), SAMPLE_CASE)
    _assert_schema_compliant(result)


# --------------------------------------------------------------------------
# Failure path -- malformed input must never raise, and must escalate.
# --------------------------------------------------------------------------
def test_validate_llm_output_missing_required_field_escalates():
    malformed = {
        "case_id": "case_broken",
        "tree_model_score": 0.5,
        "tree_model_top_features": [],
        # recommended_action deliberately omitted
        "confidence": 0.8,
        "reasoning_summary": "incomplete",
        "model_version": "test",
        "timestamp": "2026-08-26T00:00:00+00:00",
    }

    result = llm_layer.validate_llm_output(malformed, case={"case_id": "case_broken"})

    assert result["requires_human_review"] is True
    assert result["recommended_action"] == "escalate_human"
    assert "llm_validation_error" in result
    assert result["llm_validation_error"]["raw_output"] == malformed


def test_validate_llm_output_off_enum_action_escalates():
    malformed = {
        **{k: v for k, v in SAMPLE_CASE.items() if k not in ("shap_top_features", "case_facts")},
        "tree_model_top_features": [],
        "recommended_action": "cancel_subscription",  # not in the enum
        "action_scheduled_for": None,
        "confidence": 0.9,
        "reasoning_summary": "off-enum action",
        "guardrail_flags": [],
        "requires_human_review": False,
        "model_version": "test",
        "timestamp": "2026-08-26T00:00:00+00:00",
    }

    result = llm_layer.validate_llm_output(malformed)

    assert result["requires_human_review"] is True
    assert "llm_validation_error" in result


def test_get_llm_decision_never_raises_when_adapter_always_fails():
    class _AlwaysFailsAdapter(llm_layer.LLMAdapter):
        def generate(self, case: dict) -> dict:
            raise RuntimeError("simulated provider outage")

    result = llm_layer.get_llm_decision(_AlwaysFailsAdapter(), SAMPLE_CASE)

    assert result["requires_human_review"] is True
    assert result["recommended_action"] == "escalate_human"
    assert "simulated provider outage" in result["llm_validation_error"]["exception"]


def test_grounding_check_flags_reasoning_that_cites_ungiven_feature():
    """SAMPLE_CASE's top-5 is historical_ptp_honor_rate / ltv_tier /
    payment_rail. Reasoning that cites amount_vs_historical_avg (a real,
    known case feature, but NOT in this case's top-5) must be flagged --
    not blocked -- via guardrail_flags."""
    raw = {
        **llm_layer._echoed_fields(SAMPLE_CASE, "mock:test-model"),
        "recommended_action": "retry_scheduled",
        "action_scheduled_for": "2026-08-27T09:00:00+00:00",
        "confidence": 0.9,
        "reasoning_summary": (
            "Given historical_ptp_honor_rate is high and amount_vs_historical_avg looks "
            "favorable, a scheduled retry is justified."
        ),
        "guardrail_flags": [],
        "requires_human_review": False,
    }

    result = llm_layer.validate_llm_output(raw, SAMPLE_CASE)

    assert "llm_validation_error" not in result
    assert "possible_ungrounded_reasoning" in result["guardrail_flags"]
    assert result["requires_human_review"] is False  # flagged, not blocked


def test_grounding_check_does_not_false_positive_on_substring_overlap():
    """"amount" must not match inside "amount_vs_historical_avg" -- if the
    given top-5 includes the longer name, reasoning mentioning only that
    longer name must not spuriously flag as citing the shorter one."""
    case = {
        **SAMPLE_CASE,
        "shap_top_features": [
            {"feature": "amount_vs_historical_avg", "value": 0.85, "shap_value": -0.1},
        ],
    }
    raw = {
        **llm_layer._echoed_fields(case, "mock:test-model"),
        "recommended_action": "retry_now",
        "action_scheduled_for": None,
        "confidence": 0.8,
        "reasoning_summary": "amount_vs_historical_avg has a NEGATIVE shap_value of -0.1.",
        "guardrail_flags": [],
        "requires_human_review": False,
    }

    result = llm_layer.validate_llm_output(raw, case)

    assert "possible_ungrounded_reasoning" not in result["guardrail_flags"]


def test_gemini_adapter_retries_once_per_backoff_step_on_429_then_succeeds(monkeypatch):
    from google.genai import errors

    monkeypatch.setattr(llm_layer.time, "sleep", lambda _seconds: None)

    adapter = llm_layer.GeminiAdapter.__new__(llm_layer.GeminiAdapter)
    adapter.model = "gemini-3.1-flash-lite"

    class _FakeResponse:
        text = json.dumps(
            {
                "recommended_action": "retry_now",
                "action_scheduled_for": None,
                "confidence": 0.8,
                "reasoning_summary": "ok",
                "guardrail_flags": [],
                "requires_human_review": False,
            }
        )

    calls = {"n": 0}

    class _FakeModels:
        def generate_content(self, model, contents, config):
            calls["n"] += 1
            if calls["n"] < 3:
                raise errors.ClientError(429, {"error": {"message": "rate limited"}})
            return _FakeResponse()

    class _FakeClient:
        models = _FakeModels()

    adapter.client = _FakeClient()

    result = adapter.generate(SAMPLE_CASE)

    assert calls["n"] == 3  # 2 failures (429) + 1 success, matching the 2s/5s backoff schedule
    assert result["recommended_action"] == "retry_now"


def test_gemini_adapter_gives_up_after_backoff_exhausted(monkeypatch):
    from google.genai import errors

    monkeypatch.setattr(llm_layer.time, "sleep", lambda _seconds: None)

    adapter = llm_layer.GeminiAdapter.__new__(llm_layer.GeminiAdapter)
    adapter.model = "gemini-3.1-flash-lite"

    class _FakeModels:
        def generate_content(self, model, contents, config):
            raise errors.ClientError(429, {"error": {"message": "rate limited"}})

    class _FakeClient:
        models = _FakeModels()

    adapter.client = _FakeClient()

    try:
        adapter.generate(SAMPLE_CASE)
        assert False, "expected ClientError to propagate once backoff is exhausted"
    except errors.ClientError as exc:
        assert exc.code == 429

    # get_llm_decision must still turn a fully-exhausted 429 into a clean
    # escalation rather than crashing the batch.
    result = llm_layer.get_llm_decision(adapter, SAMPLE_CASE)
    assert result["requires_human_review"] is True
    assert result["recommended_action"] == "escalate_human"


def test_low_confidence_forces_human_review_even_when_schema_valid():
    class _LowConfidenceAdapter(llm_layer.LLMAdapter):
        def generate(self, case: dict) -> dict:
            return {
                **llm_layer._echoed_fields(case, "mock:test-model"),
                "recommended_action": "retry_now",
                "action_scheduled_for": None,
                "confidence": 0.1,
                "reasoning_summary": "uncertain",
                "guardrail_flags": [],
                "requires_human_review": False,
            }

    result = llm_layer.get_llm_decision(_LowConfidenceAdapter(), SAMPLE_CASE)

    assert "llm_validation_error" not in result
    assert result["requires_human_review"] is True
