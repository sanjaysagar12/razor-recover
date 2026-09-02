"""
Phase 5 -- LLM layer.

Provider-agnostic structured-decision layer over the cases Phase 4 routed to
the LLM (`routed_to_llm: true`). ClaudeAdapter and GeminiAdapter both return
the exact same dict shape (schema.LLMDecision); get_llm_adapter() picks
between them at runtime via the LLM_PROVIDER env var, so nothing else in the
pipeline needs to know which provider produced a given decision.

The LLM never acts directly -- it only proposes recommended_action.
Phase 6 guardrails get the final say on every case.

Smoke test a single case:
    python -m pipeline.llm_layer --case-file tests/fixtures/sample_case.json
"""

from __future__ import annotations

import abc
import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
from pydantic import ValidationError

import prompts
import schema

load_dotenv()

logger = logging.getLogger(__name__)

LLM_CONFIDENCE_FLOOR = float(os.environ.get("LLM_CONFIDENCE_FLOOR", "0.5"))
MAX_GENERATE_ATTEMPTS = 2  # one initial attempt + one retry, per Phase 5 spec

# Every non-SHAP case fact the model could plausibly reference in
# reasoning_summary (pipeline/shap_extract.py's feature_columns + decline_code).
# Used by _check_grounding to flag reasoning that cites a fact not in that
# case's given SHAP top-5 -- see validate_llm_output.
KNOWN_CASE_FEATURES = (
    "decline_code",
    "retry_attempt_number",
    "hours_since_last_attempt",
    "customer_tenure_days",
    "ltv_tier",
    "historical_ptp_honor_rate",
    "time_of_day_bucket",
    "day_of_week",
    "issuer_bank_risk_tier",
    "payment_rail",
    "amount",
    "amount_vs_historical_avg",
    "is_peak_execution_window",
    "prior_retry_success_count",
)

# Gemini rate-limit (HTTP 429) backoff: one initial attempt + these retries,
# in seconds. Transport-level only -- separate from, and exhausted before,
# the content-validation retry in get_llm_decision.
GEMINI_RATE_LIMIT_BACKOFF_SECONDS = (2, 5)


class LLMAdapter(abc.ABC):
    @abc.abstractmethod
    def generate(self, case: dict) -> dict:
        """case: {case_id, tree_model_score, shap_top_features, case_facts}.

        Returns a plain dict shaped like schema.LLMDecision (raw,
        unvalidated -- run it through validate_llm_output before trusting
        it). Every field is populated: the LLM decides recommended_action /
        action_scheduled_for / confidence / reasoning_summary /
        guardrail_flags / requires_human_review, and the adapter fills in
        case_id / tree_model_score / tree_model_top_features (echoed from
        `case`, not re-derived by the model) plus model_version / timestamp.
        """


def _echoed_fields(case: dict, model_version: str) -> dict:
    return {
        "case_id": case["case_id"],
        "tree_model_score": case["tree_model_score"],
        "tree_model_top_features": [
            {"feature": f["feature"], "shap_value": f["shap_value"]} for f in case["shap_top_features"]
        ],
        "model_version": model_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


class ClaudeAdapter(LLMAdapter):
    def __init__(self):
        import anthropic

        self.model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    def generate(self, case: dict) -> dict:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=prompts.SYSTEM_PROMPT,
            tools=[
                {
                    "name": "submit_decision",
                    "description": "Submit the structured recovery-action decision for this case.",
                    "strict": True,
                    "input_schema": schema.CLAUDE_TOOL_INPUT_SCHEMA,
                }
            ],
            tool_choice={"type": "tool", "name": "submit_decision"},
            messages=[{"role": "user", "content": prompts.build_user_prompt(case)}],
        )
        tool_use = next(b for b in response.content if b.type == "tool_use")
        proposed = dict(tool_use.input)
        return {**_echoed_fields(case, f"claude:{self.model}"), **proposed}

    def extract_date(self, message: str, case_context: dict) -> dict:
        """Promise-to-pay date extraction (see extract_promise_date below).
        Same tool-use pattern as generate() above, different tool/schema."""
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=prompts.PROMISE_DATE_SYSTEM_PROMPT,
            tools=[
                {
                    "name": "submit_date_extraction",
                    "description": "Submit the structured promise-to-pay date extraction result for this customer reply.",
                    "strict": True,
                    "input_schema": schema.CLAUDE_PROMISE_DATE_TOOL_SCHEMA,
                }
            ],
            tool_choice={"type": "tool", "name": "submit_date_extraction"},
            messages=[{"role": "user", "content": prompts.build_promise_date_user_prompt(message, case_context)}],
        )
        tool_use = next(b for b in response.content if b.type == "tool_use")
        proposed = dict(tool_use.input)
        return {**_echoed_date_fields(f"claude:{self.model}"), **proposed}


class GeminiAdapter(LLMAdapter):
    def __init__(self):
        from google import genai

        self.model = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    def generate(self, case: dict) -> dict:
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            system_instruction=prompts.SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=schema.GEMINI_RESPONSE_SCHEMA,
            # We pass no tools/function declarations here, so automatic
            # function calling is pure overhead -- disable it to silence the
            # SDK's AFC warning on every call.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents = prompts.build_user_prompt(case)

        delays = (0,) + GEMINI_RATE_LIMIT_BACKOFF_SECONDS
        last_exc: Exception | None = None
        for attempt, delay in enumerate(delays):
            if delay:
                logger.warning(
                    "GeminiAdapter: 429 rate limit for case_id=%s -- retry %d/%d after %ds backoff",
                    case.get("case_id"), attempt, len(delays) - 1, delay,
                )
                time.sleep(delay)
            try:
                response = self.client.models.generate_content(model=self.model, contents=contents, config=config)
                proposed = json.loads(response.text)
                return {**_echoed_fields(case, f"gemini:{self.model}"), **proposed}
            except errors.ClientError as exc:
                if getattr(exc, "code", None) == 429:
                    last_exc = exc
                    continue
                raise
        raise last_exc

    def extract_date(self, message: str, case_context: dict) -> dict:
        """Promise-to-pay date extraction (see extract_promise_date below).
        Reuses generate()'s 429 backoff schedule via _gemini_generate_text
        rather than duplicating it inline."""
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=prompts.PROMISE_DATE_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=schema.GEMINI_PROMISE_DATE_RESPONSE_SCHEMA,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents = prompts.build_promise_date_user_prompt(message, case_context)
        text = _gemini_generate_text(self.client, self.model, contents, config, case_context.get("case_id"))
        proposed = json.loads(text)
        return {**_echoed_date_fields(f"gemini:{self.model}"), **proposed}


def _gemini_generate_text(client, model: str, contents, config, log_context) -> str:
    """Shared 429-backoff retry loop for a single Gemini generate_content
    call, factored out so extract_date can reuse GeminiAdapter.generate's
    retry schedule without duplicating it (and without touching generate()
    itself, which existing tests exercise directly)."""
    from google.genai import errors

    delays = (0,) + GEMINI_RATE_LIMIT_BACKOFF_SECONDS
    last_exc: Exception | None = None
    for attempt, delay in enumerate(delays):
        if delay:
            logger.warning(
                "Gemini: 429 rate limit for case_id=%s -- retry %d/%d after %ds backoff",
                log_context, attempt, len(delays) - 1, delay,
            )
            time.sleep(delay)
        try:
            response = client.models.generate_content(model=model, contents=contents, config=config)
            return response.text
        except errors.ClientError as exc:
            if getattr(exc, "code", None) == 429:
                last_exc = exc
                continue
            raise
    raise last_exc


def get_llm_adapter() -> LLMAdapter:
    provider = os.environ.get("LLM_PROVIDER", "claude").strip().lower()
    if provider == "claude":
        return ClaudeAdapter()
    if provider == "gemini":
        return GeminiAdapter()
    raise ValueError(f"Unknown LLM_PROVIDER {provider!r} -- expected 'claude' or 'gemini'")


def _fallback_decision(case: dict, raw_output, error: str) -> dict:
    shap_top_features = case.get("shap_top_features", [])
    return {
        "case_id": case.get("case_id"),
        "tree_model_score": case.get("tree_model_score"),
        "tree_model_top_features": [
            {"feature": f["feature"], "shap_value": f["shap_value"]} for f in shap_top_features
        ],
        "recommended_action": "escalate_human",
        "action_scheduled_for": None,
        "confidence": 0.0,
        "reasoning_summary": "LLM output failed schema validation -- escalated to human review.",
        "guardrail_flags": [],
        "requires_human_review": True,
        "model_version": "validation_fallback",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "llm_validation_error": {"raw_output": raw_output, "exception": error},
    }


def _check_grounding(decision_dict: dict) -> bool:
    """True if reasoning_summary appears to cite a known case feature that
    was NOT in this case's given SHAP top-5 -- a case-insensitive,
    word-boundary scan (not a hard block; see validate_llm_output). Word
    boundaries matter here: "amount" must not match inside
    "amount_vs_historical_avg" just because it's a substring."""
    given = {f["feature"] for f in decision_dict.get("tree_model_top_features", [])}
    reasoning = decision_dict.get("reasoning_summary") or ""
    for feature in KNOWN_CASE_FEATURES:
        if feature in given:
            continue
        if re.search(r"\b" + re.escape(feature) + r"\b", reasoning, flags=re.IGNORECASE):
            return True
    return False


def validate_llm_output(raw: dict, case: dict | None = None) -> dict:
    """Parses `raw` (an adapter's generate() output) against
    schema.LLMDecision and checks recommended_action / confidence are in
    range. On ANY failure -- malformed structure, off-enum action,
    out-of-range confidence -- returns a requires_human_review=True fallback
    dict with the raw output and exception preserved under
    llm_validation_error. Never raises.

    Below LLM_CONFIDENCE_FLOOR, the decision still validates (it's schema-
    valid) but requires_human_review is forced True -- a low-confidence
    recommendation is not a validation failure, it's a case that shouldn't
    be auto-actioned.

    Also runs a grounding check (_check_grounding): if reasoning_summary
    appears to cite a case fact outside the given SHAP top-5, this does NOT
    force requires_human_review -- it appends "possible_ungrounded_reasoning"
    to guardrail_flags so it's visible in the audit log without blocking
    the case.
    """
    case = case or {}
    try:
        decision = schema.LLMDecision.model_validate(raw)
    except (ValidationError, TypeError) as exc:
        return _fallback_decision(case, raw, f"{type(exc).__name__}: {exc}")

    if decision.recommended_action not in schema.RECOMMENDED_ACTIONS:
        return _fallback_decision(case, raw, f"recommended_action {decision.recommended_action!r} not in enum")
    if not (0.0 <= decision.confidence <= 1.0):
        return _fallback_decision(case, raw, f"confidence {decision.confidence!r} out of [0, 1]")

    out = decision.model_dump()
    if _check_grounding(out) and "possible_ungrounded_reasoning" not in out["guardrail_flags"]:
        out["guardrail_flags"].append("possible_ungrounded_reasoning")
    if decision.confidence < LLM_CONFIDENCE_FLOOR:
        out["requires_human_review"] = True
    return out


def get_llm_decision(adapter: LLMAdapter, case: dict) -> dict:
    """Calls adapter.generate() and validates the result. On validation
    failure, retries generate()+validate ONCE more (a single retry of the
    whole call, since the failure may be a one-off flaky response) before
    falling back to requires_human_review=True. Never raises."""
    last_raw = None
    last_error = "generate() was never called"
    for _ in range(MAX_GENERATE_ATTEMPTS):
        try:
            raw = adapter.generate(case)
        except Exception as exc:  # noqa: BLE001 -- any adapter/SDK failure routes to fallback, not a crash
            last_raw, last_error = None, f"{type(exc).__name__}: {exc}"
            continue

        validated = validate_llm_output(raw, case)
        if "llm_validation_error" not in validated:
            return validated
        last_raw, last_error = raw, validated["llm_validation_error"]["exception"]

    return _fallback_decision(case, last_raw, last_error)


# --------------------------------------------------------------------------
# Promise-to-pay date extraction -- parses a customer's free-text reply
# (pipeline/promise_store.py's raw_customer_reply) into a structured
# commitment date. Same adapter/client/validation machinery as the SHAP-
# narration decision above (get_llm_adapter, MAX_*_ATTEMPTS retry-once
# pattern, "never raise, fall back to human review" contract) -- just a
# different prompt/schema/adapter method (extract_date instead of generate).
# --------------------------------------------------------------------------
MAX_EXTRACT_ATTEMPTS = 2  # same one-retry contract as MAX_GENERATE_ATTEMPTS

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DEFAULT_CLARIFICATION_NEEDED = "Could you give me a specific date you'd like to pay by?"


def _echoed_date_fields(model_version: str) -> dict:
    return {
        "model_version": model_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _fallback_date_extraction(raw_output, error: str) -> dict:
    return {
        "extracted_date": None,
        "confidence": 0.0,
        "ambiguous": True,
        "clarification_needed": DEFAULT_CLARIFICATION_NEEDED,
        "model_version": "validation_fallback",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "llm_validation_error": {"raw_output": raw_output, "exception": error},
    }


def validate_promise_date_output(raw: dict) -> dict:
    """Parses `raw` (an adapter's extract_date() output) against
    schema.PromiseDateExtraction. On ANY failure -- malformed structure,
    out-of-range confidence, a non-YYYY-MM-DD extracted_date -- returns an
    ambiguous=True fallback dict with the raw output and exception preserved
    under llm_validation_error, exactly like validate_llm_output does for
    decisions. Never raises.

    Also enforces "never invent a date not implied by the text" as a hard
    postcondition, not just a prompt instruction: if the model returns
    ambiguous=True but still filled in extracted_date, the date is discarded
    (logged, not treated as a validation failure -- ambiguous=True is still
    the caller-visible signal to route to clarification)."""
    try:
        parsed = schema.PromiseDateExtraction.model_validate(raw)
    except (ValidationError, TypeError) as exc:
        return _fallback_date_extraction(raw, f"{type(exc).__name__}: {exc}")

    if parsed.extracted_date is not None and not _ISO_DATE_RE.match(parsed.extracted_date):
        return _fallback_date_extraction(raw, f"extracted_date {parsed.extracted_date!r} is not YYYY-MM-DD")
    if not (0.0 <= parsed.confidence <= 1.0):
        return _fallback_date_extraction(raw, f"confidence {parsed.confidence!r} out of [0, 1]")

    out = parsed.model_dump()
    if out["ambiguous"] and out["extracted_date"] is not None:
        logger.warning(
            "extract_promise_date: model returned ambiguous=True with a non-null extracted_date=%r -- "
            "discarding the date rather than risk an unimplied guess",
            out["extracted_date"],
        )
        out["extracted_date"] = None
    if out["ambiguous"] and not out["clarification_needed"]:
        out["clarification_needed"] = DEFAULT_CLARIFICATION_NEEDED

    out["model_version"] = raw.get("model_version")
    out["timestamp"] = raw.get("timestamp")
    return out


def extract_promise_date(message: str, case_context: dict, adapter: LLMAdapter | None = None) -> dict:
    """Parses a customer's free-text promise-to-pay reply into a structured
    commitment date via the same LLM_PROVIDER the SHAP-narration decision
    uses (get_llm_adapter()), unless `adapter` is given (tests).

    case_context must include "today" (ISO date, explicit per
    prompts.PROMISE_DATE_SYSTEM_PROMPT rule 1 -- the model must never infer
    "today" itself). case_id/amount/decline_code are optional grounding
    context, passed through to the prompt when present.

    Retries once on a schema-invalid response (MAX_EXTRACT_ATTEMPTS), same
    as get_llm_decision. Never raises -- any adapter/SDK failure or
    exhausted retry degrades to ambiguous=True, confidence=0.0, so the
    caller always has a clarification question to fall back on rather than
    an unhandled exception."""
    if not case_context.get("today"):
        raise ValueError("case_context['today'] is required -- never let the model infer 'today' itself")

    adapter = adapter or get_llm_adapter()
    last_raw = None
    last_error = "extract_date() was never called"
    for _ in range(MAX_EXTRACT_ATTEMPTS):
        try:
            raw = adapter.extract_date(message, case_context)
        except Exception as exc:  # noqa: BLE001 -- any adapter/SDK failure routes to fallback, not a crash
            last_raw, last_error = None, f"{type(exc).__name__}: {exc}"
            continue

        validated = validate_promise_date_output(raw)
        if "llm_validation_error" not in validated:
            return validated
        last_raw, last_error = raw, validated["llm_validation_error"]["exception"]

    return _fallback_date_extraction(last_raw, last_error)


# --------------------------------------------------------------------------
# CLI smoke test
# --------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Run one case through the Phase 5 LLM layer.")
    parser.add_argument(
        "--case-file",
        required=True,
        help="Path to a JSON file with {case_id, tree_model_score, shap_top_features, case_facts}.",
    )
    args = parser.parse_args()

    with open(args.case_file, "r", encoding="utf-8") as f:
        case = json.load(f)

    adapter = get_llm_adapter()
    print(f"Provider: {os.environ.get('LLM_PROVIDER', 'claude')} ({type(adapter).__name__})")
    result = get_llm_decision(adapter, case)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
