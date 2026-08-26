"""
Phase 5 -- shared output schema for the LLM layer.

Single source of truth for the decision shape, in three forms:
  * LLMDecision      -- Pydantic model used to validate a fully-assembled
                         decision record (see llm_layer.validate_llm_output).
  * CLAUDE_TOOL_INPUT_SCHEMA / GEMINI_RESPONSE_SCHEMA -- the JSON-schema
    handed to each provider's structured-output API. These only cover the
    fields the LLM actually has to decide (recommended_action,
    action_scheduled_for, confidence, reasoning_summary, guardrail_flags,
    requires_human_review) -- case_id, tree_model_score,
    tree_model_top_features, model_version and timestamp are already known
    host-side (they're exactly what we handed the model as input, or
    deterministic run metadata), so the adapters echo those in rather than
    asking the LLM to re-transcribe numbers it was given. See
    pipeline/llm_layer.py for where the two halves are merged.

Claude's tool schema and Gemini's response schema are NOT interchangeable
JSON Schema dialects: Claude uses standard JSON Schema (type arrays like
["string", "null"] for nullability), Gemini's `response_schema` is an
OpenAPI-style subset (single `type` string + `nullable: true`, no `anyOf`
type-unions for optionality). Keep both in sync by hand when the shape
changes -- they're small and this stays easier to read than a converter.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

RECOMMENDED_ACTIONS: tuple[str, ...] = (
    "retry_now",
    "retry_scheduled",
    "no_retry_prompt_update",
    "escalate_human",
    "prompt_alt_payment",
)

RecommendedAction = Literal[
    "retry_now",
    "retry_scheduled",
    "no_retry_prompt_update",
    "escalate_human",
    "prompt_alt_payment",
]


class ShapFeature(BaseModel):
    feature: str
    shap_value: float


class LLMDecision(BaseModel):
    """Validated shape of a Phase 5 decision record -- identical regardless
    of which provider (Claude or Gemini) produced it."""

    case_id: str
    tree_model_score: float
    tree_model_top_features: list[ShapFeature]
    recommended_action: RecommendedAction
    action_scheduled_for: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning_summary: str
    guardrail_flags: list[str] = Field(default_factory=list)
    requires_human_review: bool = False
    model_version: str
    timestamp: str


_PROPOSED_PROPERTIES_BASE = {
    "recommended_action": {"type": "string", "enum": list(RECOMMENDED_ACTIONS)},
    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    "reasoning_summary": {"type": "string"},
    "guardrail_flags": {"type": "array", "items": {"type": "string"}},
    "requires_human_review": {"type": "boolean"},
}

_PROPOSED_REQUIRED = [
    "recommended_action",
    "action_scheduled_for",
    "confidence",
    "reasoning_summary",
    "guardrail_flags",
    "requires_human_review",
]

# Claude tool `input_schema` -- strict-tool-use compliant (additionalProperties:
# false + required covering every listed property).
CLAUDE_TOOL_INPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        **_PROPOSED_PROPERTIES_BASE,
        "action_scheduled_for": {
            "type": ["string", "null"],
            "description": "ISO8601 timestamp if recommended_action is retry_scheduled, else null.",
        },
    },
    "required": _PROPOSED_REQUIRED,
    "additionalProperties": False,
}

# Gemini `response_schema` -- OpenAPI-style subset: no type arrays, use
# `nullable` instead (matches google-genai's types.Schema field set).
GEMINI_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        **_PROPOSED_PROPERTIES_BASE,
        "action_scheduled_for": {
            "type": "string",
            "nullable": True,
            "description": "ISO8601 timestamp if recommended_action is retry_scheduled, else null.",
        },
    },
    "required": _PROPOSED_REQUIRED,
}
