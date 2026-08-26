# Phase 5 — LLM Layer

Reasons over the cases Phase 4 flagged as ambiguous (`routed_to_llm: true`)
and proposes a `recommended_action`. The LLM never acts directly — Phase 6
guardrails get the final say on every case, LLM-touched or not.

## Files

| File | Purpose |
|---|---|
| `schema.py` | `LLMDecision` Pydantic model + the JSON schemas handed to each provider's structured-output API |
| `prompts.py` | System prompt, few-shot examples, user-prompt assembly — shared by both adapters |
| `llm_layer.py` | `ClaudeAdapter`, `GeminiAdapter`, `get_llm_adapter()` factory, `validate_llm_output()`, `get_llm_decision()` |
| `run_phase5.py` | Batch orchestrator — reads `logs/phase4_output.json`, runs routed cases through the LLM layer, writes `logs/phase5_output.json` |
| `test_llm_layer.py` | Schema-compliance + failure-path tests |

## Switching providers

Set `LLM_PROVIDER` in `.env` — no code changes needed:

```
LLM_PROVIDER=claude   # or: gemini
```

Everything else (`ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL`,
`GEMINI_API_KEY`/`GEMINI_MODEL`, `LLM_CONFIDENCE_FLOOR`) is read from `.env`.
`LLM_PROVIDER` is the only thing that decides which adapter runs — nothing
outside `llm_layer.py` references "claude" or "gemini" by name.

## Fallback behavior

`get_llm_decision()` calls the adapter, validates the result against
`schema.LLMDecision`, and:

- **On any failure** (adapter exception, malformed JSON, off-enum action,
  out-of-range confidence) — retries the whole call **once**, then returns a
  fallback record with `requires_human_review: true`,
  `recommended_action: "escalate_human"`, and an `llm_validation_error` field
  carrying the raw output and the exception message. Never raises.
- **On success but low confidence** (`confidence < LLM_CONFIDENCE_FLOOR`,
  default `0.5`) — the record still validates (it's schema-valid) but
  `requires_human_review` is forced to `true`. A low-confidence
  recommendation isn't a validation failure, it's a case that shouldn't be
  auto-actioned.

This fallback path is exercised for real by rate limits/outages, not just in
tests — running the full batch against a free-tier Gemini key, for example,
will show 429s escalating cleanly rather than crashing the run.

## Design note: who fills in which fields

The LLM only decides `recommended_action`, `action_scheduled_for`,
`confidence`, `reasoning_summary`, `guardrail_flags`, `requires_human_review`
— that's the schema handed to the provider APIs. `case_id`,
`tree_model_score`, `tree_model_top_features`, `model_version`, and
`timestamp` are echoed in by the adapter from data already known host-side,
rather than asking the model to re-transcribe numbers it was given (avoids a
class of hallucination risk for no benefit). The final validated record
still contains all fields from the Phase 5 spec's schema.

## Smoke test a single case

```bash
python -m pipeline.llm_layer --case-file sample_case.json
```

`sample_case.json` must contain `{case_id, tree_model_score,
shap_top_features, case_facts}` — see `SAMPLE_CASE` in `test_llm_layer.py`
for the exact shape.

## Running the batch

```bash
python pipeline/run_phase5.py
```

Reads `logs/phase4_output.json`, writes `logs/phase5_output.json`. Cases
with `routed_to_llm: false` pass through with their Phase 4
`template_action` as `final_action` and `llm_output: null` — they never call
the LLM.

## Tests

```bash
python -m pytest pipeline/test_llm_layer.py -v
```

Live-calls whichever provider has a real key set in `.env` (a placeholder
value like `your_anthropic_api_key_here` is treated as "not set"); falls
back to a mocked adapter for the other provider so the suite never hard-fails
on a missing key.
