# ARCHITECTURE.md — razor-recover

This document describes the system **as it exists in code today**, not as originally
planned in the phase-by-phase build notes (`PHASE7_REPORT.md`, `README_WEBHOOK.md`,
module docstrings referencing "Phase N"). Where the code diverges from what a comment,
docstring, or variable name implies, that is called out explicitly in
[Discrepancies & known gaps](#discrepancies--known-gaps) rather than documented at face value.

---

## 1. System overview

razor-recover is a Flask webhook receiver (`webhook_receiver.py`) that sits in front of
a Razorpay account and decides what to do about failed payments. When Razorpay posts a
`payment.failed` (or `subscription.pending`/`subscription.halted`) event, the case is
mapped into a feature dict, scored by a pre-trained tree model, explained with SHAP, and
routed either to a deterministic template action or to an LLM (Claude or Gemini) that
reasons over the SHAP explanation and proposes a recovery action. Every proposal —
template or LLM — passes through a deterministic guardrail layer that can force a
different final action or flag the case for human review; guardrails, never the model,
have final say. The approved action is then executed against the real Razorpay API
(sending a payment link — Razorpay has no force-retry API) or logged as scheduled, and a
row is appended to `logs/webhook_audit_log.csv`. Separately, the system can proactively
solicit a "when can you pay?" commitment from the customer (a promise-to-pay, or PTP):
an eligibility gate decides whether to offer this at all, a customer's free-text reply is
parsed into a structured date by the LLM, that extraction runs through its own guardrail
pass, and an approved date results in a fresh Razorpay payment link expiring at end-of-day
on the promised date. Payment-success/failure webhooks are then used to mark promises
honored or broken, which feeds a per-customer risk-tier escalation ladder that loops back
into both the confidence gate (routing) and the guardrail layer (blocking self-service PTP
for restricted customers). A read-only Flask-served dashboard (`/dashboard`,
`/conversations`) polls JSON APIs to show all of this as it happens.

---

## 2. High-level flow diagram

```mermaid
flowchart TD
    subgraph Entry1["Entry point 1: payment-failure webhook"]
        A[POST /webhook/razorpay] --> A1{HMAC-SHA256\nsignature valid?}
        A1 -- no --> A1R[400, log signature_rejected]
        A1 -- yes --> A2[Parse JSON, classify event_type]
        A2 --> A3[map_payload_to_case\ndecline_code + decline_code_bucket\ncustomer_history lookup/create\ncustomer_directory upsert]
    end

    A3 --> B[shap_extract.score_new_case\ntree model probability + top-5 SHAP features]
    B --> C[confidence_gate.route_case\nprobability band ∪ ambiguous_code ∪ watch-tier]
    C -->|not routed| D1[Template action\nretry_now / retry_scheduled / no_retry_prompt_update]
    C -->|routed_to_llm| D2[llm_layer: ClaudeAdapter/GeminiAdapter\nSHAP-grounded recommended_action]
    D2 --> D3[validate_llm_output\nschema + confidence-floor + grounding check]
    D1 --> E[guardrails.apply_guardrails\nordered rule list, first override wins]
    D3 --> E
    E --> F[ptp_trigger.should_offer_ptp\n(transparency-only, does not affect final_action)]
    F --> G[execute_action.execute_action\npayment_link / pending_retries log / no-op]
    G --> H[(logs/webhook_audit_log.csv)]

    subgraph Entry2["Entry point 2: PTP reply intake"]
        P0[POST /api/promise-reply] --> P1[ptp_trigger.should_offer_ptp\neligibility gate — first reply only]
        P1 -- rejected --> P1R[200 rejected, audit row logged]
        P1 -- offered/continuing --> P2[promise_store.create_promise\nraw reply saved durably]
        P2 --> P3[llm_layer.extract_promise_date\nstructured YYYY-MM-DD extraction]
        P3 -->|ambiguous / low confidence| P4[Clarification loop\ncapped at MAX_CLARIFICATION_ROUNDS,\nthen fixed 24h fallback]
        P3 -->|clean extraction| P5[guardrails.apply_ptp_guardrails\nwindow cap / past date / NPCI peak / confidence floor]
        P5 -->|approved| P6[execute_action.execute_promise_reschedule\nnew payment link, expire_by = EOD promised date]
        P5 -->|rejected/pending_clarification| P7[human review or re-ask customer]
        P6 --> H
        P7 --> H
        P4 --> H
    end

    subgraph Entry3["Entry point 3: PTP outcome webhooks"]
        R1[payment.captured / payment_link.paid] --> R2[ptp_outcomes.handle_payment_captured\nmatch by notes.promise_id or payment_link_id]
        R2 --> R3[promise honored / late recovery / no-op]
        R3 --> R4[customer_ptp_stats.record_promise_resolution\nrisk-tier recompute: normal → watch → restricted]
        R4 -.->|current_risk_tier feeds back| C
        R4 -.->|current_risk_tier feeds back| E
        R5[Background thread, 5 min] --> R6[ptp_outcomes.check_expired_promises\npromise past due → broken]
        R6 --> R4
    end
```

---

## 3. Component reference

### `webhook_receiver.py` — Flask app, entry point
The only executable entry point (`python webhook_receiver.py`, port 5555, tunneled at
`https://razor-recover.heracle.fit`). Responsibilities:
- `verify_razorpay_signature` — HMAC-SHA256 over the **raw** request body, constant-time
  compared against `X-Razorpay-Signature`. Runs before any JSON parsing.
- `map_payload_to_case` — turns a verified webhook body into the case-feature dict every
  downstream stage consumes. Has a side effect: creates a `customer_history` row on
  first-seen customers, and always upserts `customer_directory` (email↔customer_key).
  Populates both `decline_code` (synthetic vocabulary, via `RAZORPAY_REASON_TO_DECLINE_CODE`)
  and `decline_code_bucket`/`decline_code_is_ambiguous` (via `decline_code_mapper`) — see
  [§4](#4-the-two-decline-code-vocabularies-a-deliberate-two-layer-design).
- `process_recovery_case` / `process_recovered_case` — thin wrappers that hand the mapped
  case to `run_case.run_recovery_case` / `run_recovered_case`. The dashboard's
  `/api/trigger-test-case` and `/api/trigger-webhook-shaped` endpoints call the exact same
  `run_case` functions with an operator-supplied case instead of a real payload — no
  pipeline logic is duplicated between the real and synthetic paths.
- Route table (all return HTTP 200 except signature failures/malformed JSON, so Razorpay
  never retry-storms the endpoint on an internal error):
  - `POST /webhook/razorpay` — dispatches on `event`: `payment.failed` /
    `subscription.pending` / `subscription.halted` → full recovery pipeline (plus, for
    `payment.failed` only, an observational, exception-swallowed call into
    `ptp_outcomes.handle_payment_failed`); `subscription.charged` → `run_recovered_case`;
    `payment.captured` / `payment_link.paid` → `ptp_outcomes.handle_payment_captured`;
    anything else → acked as `ignored`.
  - `GET /health`
  - `POST /api/promise-reply` — PTP reply intake (see below).
  - `POST /api/trigger-test-case`, `POST /api/trigger-webhook-shaped` — synthetic case
    triggers for the dashboard, rate-limited together at 20 req/60s (process-global,
    in-memory, resets on restart).
  - `GET /api/audit-log`, `/api/webhook-log`, `/api/summary`, `/api/case-detail/<case_id>`,
    `/api/customers`, `/api/customer/<email>/conversations` — read-only JSON for the
    dashboards.
  - `POST /api/ptp/check-expired` — manual trigger for the deadline sweep.
  - `POST /api/reset-logs`, `POST /api/reset-conversations` — demo-state resets (see
    inline docstrings for exactly what each clears).
  - `GET /dashboard`, `GET /conversations` — serve the static HTML files verbatim
    (`Response(path.read_text(), mimetype="text/html")`); **no templating, no
    authentication** (removed deliberately, per the module's own comment).
- `_startup_checks` — refuses to start without `RAZORPAY_WEBHOOK_SECRET`, warms the
  model/SHAP background/confidence-gate band once, starts the PTP expiry background thread.

### `pipeline/run_case.py` — shared orchestrator
Extracted so the real webhook path and the dashboard's synthetic triggers call *identical*
pipeline code. Key functions:
- `run_recovery_case(case_facts, event_type, source)` — the 6-step pipeline: (1) SHAP
  score, (2) confidence-gate routing, (3) LLM layer if routed (or `MalformedTestAdapter`
  when `case_facts["_force_malformed_llm"]` is set — a dashboard-only test hook a real
  webhook can never trigger), (4) `run_batch.build_audit_row` (which itself calls
  `guardrails.apply_guardrails`), (5) `ptp_trigger.should_offer_ptp` (transparency-only,
  logged but never changes `final_action`), (6) `execute_action.execute_action`, then
  `customer_history.record_payment_outcome(recovered=False)` and an audit-row append.
- `run_recovered_case` — logs a `subscription.charged` event as a `recovered` row with no
  pipeline routing/execution (nothing to decide — the payment already succeeded).
- `run_promise_reschedule` — executes a guardrail-approved PTP date via
  `execute_action.execute_promise_reschedule`, updates the `promises` row
  (`payment_link_id` + `STATUS_SCHEDULED` on success, `mark_promise_reschedule_failed` on
  failure), and appends its own audit row.
- `build_error_row` / `append_webhook_audit_row` — a pipeline exception never crashes the
  webhook handler; it degrades to a `final_action="error_requires_human_review"` row.
- Owns `WEBHOOK_AUDIT_COLUMNS` (see [§5](#5-audit--log-schemas)) and the
  reindex-on-schema-growth migration used every time a new column is appended.

### `pipeline/decline_code_mapper.py` — real-vocabulary decline bucketing
Maps a **real** Razorpay `error_reason`/`error_code` to the model's training-time
3-value taxonomy: `CLEAR_SOFT` / `CLEAR_HARD` / `AMBIGUOUS` (sourced from Razorpay's
published error list, "fetched 2026-08-28", explicitly labeled best-effort, not ground
truth). `map_razorpay_error_reason` returns `{decline_code_bucket, is_ambiguous, matched}`;
unrecognized reasons fall through to `AMBIGUOUS`/`is_ambiguous=True` rather than a
confident guess. This is one of **two intentionally separate mapping layers** — see
[§4](#4-the-two-decline-code-vocabularies-a-deliberate-two-layer-design).

### `pipeline/shap_extract.py` — tree-model scoring + SHAP
Loads the primary model selected out-of-band (`models/artifacts/model_metadata.json`),
fits a `shap.LinearExplainer` (logistic regression) or `shap.TreeExplainer` (XGBoost/
sklearn ensembles, `model_output="probability"`) against the full 280-row
train+holdout pool, and runs an additivity sanity check (`base_value + Σ shap == predict_proba`,
tolerance `1e-4`, 10 sampled rows) at context-build time — raises `AssertionError` if it
fails. Public surface:
- `get_shap_top_features` / `get_case_facts` / `get_tree_model_score` — batch-only, keyed
  by `case_id` already present in `train.csv`/`holdout.csv`.
- `score_new_case(case_facts)` — the live-webhook path: scores an arbitrary case dict
  against the same cached explainer/background, returns
  `{tree_model_score, shap_top_features}`.
- `get_scores_df` — feeds `confidence_gate.compute_probability_band`.

### `pipeline/confidence_gate.py` — routing decision
`route_case` routes a case to the LLM layer if **any** of three triggers fire (all always
evaluated, not short-circuited):
1. **`probability_band`** — score falls within the **42.5th–57.5th percentile** of the
   batch's own score distribution (`PROB_BAND_LOWER_PCTILE`/`PROB_BAND_UPPER_PCTILE`), a
   relative slice, not a fixed probability cutoff (documented reason: logistic-regression
   scores are compressed to roughly 0.06–0.71 on this dataset, so a fixed band swallowed
   ~60% of cases regardless of real ambiguity). The band is fit once per process
   (`run_case.get_probability_band`, cached) against rows *excluding* the
   ambiguous-code trigger's own matches, so that cluster can't skew the percentile cutoffs.
2. **`ambiguous_code`** — decline-code prefix `"05"` (`AMBIGUOUS_DECLINE_CODES`) by
   default, or the caller-supplied `is_ambiguous` override (real webhook cases pass
   `decline_code_mapper`'s bucket-based flag instead, since a real `error_reason` never
   matches the synthetic `"05"` prefix).
3. **`watch_tier_customer`** (Phase 17) — `risk_tier == "watch"` always routes to the LLM,
   regardless of score. `"restricted"` does **not** change routing here (guardrails
   overrides `final_action` for restricted customers regardless of what routing/LLM decide).

When not routed, `_template_action` picks `retry_now` (score > band_high),
`retry_scheduled` (otherwise), or `no_retry_prompt_update` for a hard decline
(`guardrails.HARD_DECLINE_CODES`) regardless of score — a template action deliberately
does not rely on the downstream guardrail layer to correct an obviously-wrong default.

### `pipeline/llm_layer.py` — LLM decision + PTP date extraction
Two independent structured-output flows, both provider-agnostic (`LLM_PROVIDER` env var
selects `ClaudeAdapter` or `GeminiAdapter`; Claude uses forced tool-use, Gemini uses
`response_schema` + `response_mime_type=application/json`):

1. **SHAP-grounded recovery-action decision** (`get_llm_decision`) — the system prompt
   (`prompts.SYSTEM_PROMPT`) instructs the model to reason **only** from
   `shap_top_features`, state each SHAP value's sign/direction explicitly, and set
   `requires_human_review=true` whenever it isn't confident, before submitting via a
   strict tool schema (`schema.CLAUDE_TOOL_INPUT_SCHEMA` / `GEMINI_RESPONSE_SCHEMA`).
   `validate_llm_output` then: (a) parses against `schema.LLMDecision`, falling back to
   `requires_human_review=True, recommended_action="escalate_human"` on **any** schema/enum/
   range failure; (b) forces `requires_human_review=True` if `confidence <
   LLM_CONFIDENCE_FLOOR` (default 0.5) even on an otherwise-valid decision; (c) runs
   `_check_grounding` — a word-boundary scan for a known case feature cited in
   `reasoning_summary` that wasn't in the given SHAP top-5 — appending
   `"possible_ungrounded_reasoning"` to `guardrail_flags` (visible in the audit log, does
   **not** block the case). One retry on schema failure (`MAX_GENERATE_ATTEMPTS=2`);
   never raises.

2. **Promise-date extraction** (`extract_promise_date`) — a separate system prompt
   (`prompts.PROMISE_DATE_SYSTEM_PROMPT`) parses a customer's free-text reply into
   `{extracted_date, confidence, ambiguous, clarification_needed}` against an explicit
   `today` (never inferred by the model). `validate_promise_date_output` enforces
   `YYYY-MM-DD` format and a hard postcondition: if the model returns `ambiguous=True`
   *with* a non-null date anyway, the date is discarded (logged) rather than trusted.
   Same one-retry-then-fallback contract; fallback is `ambiguous=True, confidence=0.0`.

### `pipeline/guardrails.py` — the deterministic safety net
Two independent, structurally identical rule engines in one module (same
"every rule always evaluated, first rule in list order whose override sets the winning
field, others still recorded in `guardrail_flags`" contract):

**`GUARDRAIL_RULES`** (recovery-action decisions), evaluated in this exact order:

| # | Rule | Fires when | Override |
|---|------|-----------|----------|
| 1 | `customer_risk_restricted` | `current_risk_tier == "restricted"` | forces `final_action="escalate_human"`, `requires_human_review=True` |
| 2 | `hard_decline_excluded` | `decline_code` ∈ `{lost_card, stolen_card, restricted_card, invalid_account}` (synthetic literal strings) | forces `final_action="no_retry_prompt_update"` |
| 3 | `npci_retry_cap_reached` | `payment_rail=="upi_autopay"` and `retry_attempt_number >= 4` | forces `no_retry_prompt_update` |
| 4 | `npci_peak_window` | `payment_rail=="upi_autopay"` and the proposal's `action_scheduled_for`, converted to IST, falls in `[10:00, 13:00)` | forces `no_retry_prompt_update` |
| 5 | `network_retry_cap_exceeded` | `cumulative_retries_this_txn >= 5` | forces `no_retry_prompt_update` |
| 6 | `requires_human_review_floor` | `recommended_action` not in the enum, or `confidence < 0.4` | escalation-only: sets `requires_human_review=True`, never touches `final_action` |

Rule 1 is listed ahead of rule 2 specifically so a restricted customer's hard-decline case
still shows both flags in the audit trail but routes to `escalate_human`, not
`no_retry_prompt_update`.

**`PTP_GUARDRAIL_RULES`** (promise-date extractions), separate list because there is no
`recommended_action` to override here:

| # | Rule | Fires when | Override |
|---|------|-----------|----------|
| 1 | `window_cap_check` | promised date is more than 30 days out (`PTP_WINDOW_CAP_DAYS`) | `guardrail_status="rejected_window_cap"`, `final_action=no_retry_prompt_update`, `requires_human_review=True` |
| 2 | `past_date_check` | promised date is before `case["today"]` | `guardrail_status="rejected_past_date"`, `final_action=no_retry_prompt_update`, `routed_to_clarification=True` |
| 3 | `npci_peak_window_check` | `payment_rail=="upi_autopay"` and `extraction["action_scheduled_for"]` (IST) falls in the peak window | `guardrail_status="adjusted"`, computes `adjusted_date` — **see discrepancy below, this rule cannot fire in the current data flow** |
| 4 | `low_confidence_gate` | `confidence < 0.6` (`PTP_CONFIDENCE_FLOOR`) or `ambiguous=True` | `guardrail_status="pending_clarification"`, `final_action=pending_clarification`, `routed_to_clarification=True` |

A documented, intentionally-unimplemented rule: **`pattern_conflict_check`** (compare a
promised date against a customer's historical day-of-month/time-of-day success pattern) —
skipped because `customer_history` tracks no such per-customer pattern; the module
docstring explicitly says it won't invent state to support a rule with nothing real to
check against.

### `pipeline/ptp_trigger.py` — PTP offer-eligibility gate
`should_offer_ptp(case)` answers "should this case even be offered a PTP conversation" —
a prior, narrower question than either guardrail pass above, and deliberately not folded
into `guardrails.py`. First-match-wins evaluation order:
1. `restricted_tier` (via `customer_ptp_stats.get_risk_tier`) → **False**, absolute veto.
2. `hard_decline` (via `decline_code_mapper`'s bucket) → **False**.
3. `open_promise_exists` (via `promise_store.has_open_promise`) → **False**.
4. `high_ltv_first_failure` (`ltv_tier=="high"` and `retry_attempt_number<=1`) → **True**.
5. `insufficient_funds_code` (`decline_code` ∈ `{insufficient_funds, 51_insufficient_funds}`) → **True**.
6. `approaching_retry_cap` (one attempt before the NPCI/network cap) → **True**.
7. `first_failure_awaiting_auto_retry` (`retry_attempt_number<=1`, nothing else matched) → **False**.
8. `retry_failed_once` (fallback for everything else) → **True**.

Returns `{offer_ptp, trigger_category, reason}` — always with a human-readable reason, not
just a bool. Called from two places: `run_case.run_recovery_case` (transparency-only, logged
to the audit row, never changes `final_action`), and `webhook_receiver.api_promise_reply`
(a real gate — a reply for an ineligible case is rejected with a `ptp_reply_rejected` audit row).

### `pipeline/promise_store.py` — the `promises` table (SQLite, `data/customer_history.db`)
Schema (one row per customer reply, keyed by a generated UUID, **not** by `case_id`, so a
case with multiple replies has multiple rows):

| Column | Type | Notes |
|---|---|---|
| `promise_id` | TEXT PK | UUID4 |
| `case_id` | TEXT NOT NULL | |
| `customer_id` | TEXT | raw id supplied at `/api/promise-reply` intake |
| `raw_customer_reply` | TEXT NOT NULL | stored verbatim before any processing |
| `extracted_date` | TEXT | `YYYY-MM-DD`, NULL until extraction runs |
| `extraction_confidence` | REAL | |
| `guardrail_status` | TEXT | `apply_ptp_guardrails`' verdict |
| `payment_link_id` | TEXT | set only on a successful reschedule execution |
| `outcome` | TEXT NOT NULL DEFAULT 'pending' | `pending / ambiguous / no_reply / reschedule_failed / honored / broken` |
| `created_at` | TEXT NOT NULL | |
| `resolved_at` | TEXT | set only by `mark_promise_honored`/`mark_promise_broken` |
| `clarification_round` | INTEGER NOT NULL DEFAULT 0 | Phase 15, added via `ALTER TABLE` migration |
| `status` | TEXT NOT NULL DEFAULT 'pending' | `pending / clarifying / scheduled / fallback / requires_human_review` — independent lifecycle from `outcome` |
| `late_recovery_at` | TEXT | Phase 16, added via `ALTER TABLE` migration |

Key behaviors: `create_promise` always runs before extraction so the raw reply is durable
even if extraction fails; `has_open_promise` treats `pending`/`ambiguous`/`no_reply` as
"still open" (but **not** `reschedule_failed`, treated as a dead end, not something
actively awaiting the customer); `get_expired_pending_promises` requires
`payment_link_id IS NOT NULL` (a rejected/clarifying promise never got a link and must
never be reported as "broken"). `MAX_CLARIFICATION_ROUNDS = 2`.

### `pipeline/customer_ptp_stats.py` — the `customer_ptp_stats` table + risk-tier ladder
Schema (one row per `customer_id`, upserted only when a promise resolves — never on
pending/ambiguous):

| Column | Type |
|---|---|
| `customer_id` | TEXT PK |
| `promises_made` | INTEGER NOT NULL DEFAULT 0 |
| `promises_honored` | INTEGER NOT NULL DEFAULT 0 |
| `historical_ptp_honor_rate` | REAL NOT NULL DEFAULT 0.0 |
| `current_risk_tier` | TEXT NOT NULL DEFAULT 'normal' |
| `created_at` / `updated_at` | TEXT NOT NULL |

`compute_risk_tier` (pure function) transition rules: `restricted` never auto-clears (no
admin reset endpoint exists yet — see discrepancies); `normal → watch` when
`historical_ptp_honor_rate < 0.5` and `promises_made >= 2`; `normal → restricted` directly
when that same condition coincides with the two most recent promises both broken;
`watch → restricted` on a second **consecutive** broken promise; `watch → normal` recovery
only when the tier coming into the call was already `watch` and the latest promise was
honored. `get_risk_tier(customer_id)` defaults to `"normal"` for `None`/unseen customers.

### `pipeline/customer_history.py` — the `customer_history` table
Schema (one row per `customer_key` = Razorpay `customer_id`, or `sub:<subscription_id>`
fallback, or no row at all if neither exists on the payload):

| Column | Type |
|---|---|
| `customer_key` | TEXT PK |
| `tenure_start_date` | TEXT NOT NULL |
| `ltv_tier` | TEXT NOT NULL (default `"medium"`) |
| `historical_ptp_honor_rate` | REAL NOT NULL (default `0.5`) |
| `prior_retry_success_count` | INTEGER NOT NULL DEFAULT 0 |
| `total_retry_outcomes` | INTEGER NOT NULL DEFAULT 0 |
| `created_at` / `updated_at` | TEXT NOT NULL |

`get_or_create_customer` returns `(fields, is_first_seen)` — a first-seen customer gets
neutral training-distribution-midpoint defaults, not a guess at real behavior; this
distinction is preserved end-to-end as `customer_history_source` in the audit row.
`record_payment_outcome` recomputes `historical_ptp_honor_rate` as a plain running rate
(`prior_retry_success_count / total_retry_outcomes`).

### `pipeline/customer_directory.py` — the `customer_directory` table
Pure identity lookup, deliberately separate from `customer_history` (which holds scoring
features). Schema: `id PK, email, contact, customer_key, first_seen_at, last_seen_at`,
unique-indexed on `(email, customer_key)` since one email can map to multiple
`customer_key`s and vice versa. Filled from **every** webhook via
`map_payload_to_case` (not just the chat path), no-op/logged if `email` or `customer_key`
is missing. Backs `/api/customers` and `/api/customer/<email>/conversations`.

### `pipeline/ptp_outcomes.py` — honor/break tracking
Three entry points called from `webhook_receiver.py`:
- `handle_payment_captured` (`payment.captured`/`payment_link.paid`) — matches a promise
  via `notes.promise_id` first, `payment_link_id` as fallback; a match on a `pending`
  promise → `honored` + stats update; a match on an already-`broken` promise → records
  `late_recovery_at` only, **does not** re-resolve `outcome` or double-count in stats;
  anything else → logged no-op (webhook redelivery must be idempotent).
- `handle_payment_failed` (`payment.failed`) — **observational only**; never marks a
  promise broken (a customer may still retry the same link before the deadline). Called
  from the main webhook route alongside — not instead of — the recovery pipeline, wrapped
  in try/except so it can never turn a normal `payment.failed` into a 500.
- `check_expired_promises` — the only path allowed to mark a promise `broken`: `outcome
  == pending`, has a `payment_link_id`, and `extracted_date` (IST) is in the past. Runs on
  a 5-minute daemon-thread timer (`start_background_expiry_checker`, started once at
  process startup) and is independently callable (`POST /api/ptp/check-expired`).
- Writes every transition to `logs/ptp_outcome_log.csv`, deliberately **not**
  `logs/audit_log.csv` (see [§5](#5-audit--log-schemas)).

### `pipeline/execute_action.py` — real Razorpay execution
Only ever called for live/dashboard-triggered cases (`run_batch.py`'s synthetic
simulation never executes anything). Central documented fact: **Razorpay has no
merchant-facing "retry this payment" or "force-charge" API** — confirmed against the
Razorpay docs and the installed SDK's method list. `retry_now` and `prompt_alt_payment`
both actually execute by creating a Razorpay **Payment Link**
(`client.payment_link.create`) — the closest real, callable substitute — and this
substitution is written into every execution's `execution_detail` string so it's never
silently lost. `retry_scheduled` does not schedule anything real either: it appends a row
to `logs/pending_retries.csv` and stops there (**no scheduler/cron exists** — see
discrepancies). `no_retry_prompt_update`/`escalate_human` are terminal: no API call.
`execute_promise_reschedule` follows the same "no dedicated API, use a Payment Link"
pattern for PTP: creates a link with `expire_by` set to 23:59:59 IST on the promised date
and `notes={"promise_id": ...}` (so `ptp_outcomes.find_open_promise` can match the
resulting payment back to this exact promise). Every function here returns a result dict
and never raises — a failed Razorpay API call becomes `execution_status="failed"` in the
audit trail, never a crash.

### `pipeline/razorpay_client.py`
`get_client()` refuses to run against anything but a `rzp_test_`-prefixed key — a hard
safety check against accidentally pointing this at a live account.

### `pipeline/run_batch.py` — synthetic-batch simulation (offline, not part of the live path)
Runs the full pipeline (minus `execute_action`, minus anything live-webhook-specific)
against the frozen 280-row `train.csv`+`holdout.csv` pool, writes
`logs/audit_log.csv`, and computes a pipeline-vs-naive-baseline simulation
(`compute_simulation`/`compute_three_scenario_simulation`) written to
`demo/pitch_numbers.md`. The naive baseline is deliberately run through the *same*
guardrail rules as the real pipeline (documented at length in the module docstring) so
"pipeline beats baseline" is a claim about avoided wasted retries and net-of-cost revenue,
not an artifact of the baseline being allowed to cheat past compliance rules. This module
and its output file are **completely disjoint** from the live webhook path — see
[§5](#5-audit--log-schemas).

### `pipeline/schema.py` / `pipeline/prompts.py`
`schema.py` is the single source of truth for `LLMDecision` and `PromiseDateExtraction` —
a Pydantic model plus hand-synced JSON-schema variants for Claude's strict tool-use and
Gemini's OpenAPI-style `response_schema` (the two dialects are not interchangeable, so
these are kept manually in sync, by design, per the module docstring). `prompts.py` holds
`SYSTEM_PROMPT` (SHAP-grounded decision, with explicit sign-direction and
grounding-discipline rules) and `PROMISE_DATE_SYSTEM_PROMPT` (date extraction, with an
explicit "end of the month resolves to one exact day, but 'this month' alone does not"
rule) plus the user-prompt assembly functions.

### `dashboard/index.html` and `dashboard/conversations.html`
Both are served **verbatim as static HTML** by Flask (`Response(path.read_text())`) — no
server-side templating, no injected data, no authentication. All data is client-rendered:
inline `<script>` polls the JSON APIs via `fetch()` on a 5-second interval
(`REFRESH_MS = 5000` in `index.html`, pausable via a UI toggle) for `/api/summary`,
`/api/audit-log`, `/api/webhook-log`; `index.html` also drives the synthetic-case trigger
UI (`/api/trigger-test-case` / `/api/trigger-webhook-shaped`) and the per-case detail
modal (`/api/case-detail/<id>`, which also lets an operator submit a promise reply on the
case's behalf via `/api/promise-reply`). `conversations.html` is a separate page (not
linked data-wise to `index.html` beyond sharing the same backend) driven by
`/api/customers` and `/api/customer/<email>/conversations`, with its own reply box hitting
the same `/api/promise-reply` endpoint.

---

## 4. The two decline-code vocabularies (a deliberate two-layer design)

This system maintains **two intentionally separate mapping tables** from a real Razorpay
`error_reason` string, and conflating them is a category error:

1. **`webhook_receiver.RAZORPAY_REASON_TO_DECLINE_CODE`** — a small (~20-entry), literal
   dict mapping a handful of real Razorpay reason strings (`"insufficient_balance"`,
   `"stolen_card"`, `"do_not_honor"`, ...) onto the **synthetic** decline-code vocabulary
   the model was actually trained on (`insufficient_funds`, `05_do_not_honor`,
   `stolen_card`, ...). Unrecognized reasons default to `"generic_decline"`. This feeds
   the `decline_code` field, which `guardrails.HARD_DECLINE_CODES` and
   `confidence_gate.AMBIGUOUS_DECLINE_CODES`'s prefix match check against — both of which
   only recognize the small synthetic vocabulary.

2. **`pipeline/decline_code_mapper.py`** — a much larger (~90-entry) table sourced from
   Razorpay's real, published error-reason list, bucketing directly into the model's
   3-value **categorical feature** `CLEAR_SOFT`/`CLEAR_HARD`/`AMBIGUOUS`. This feeds
   `decline_code_bucket` and the authoritative `decline_code_is_ambiguous` flag that
   `confidence_gate.route_case` actually routes on for real traffic (bypassing the
   synthetic-prefix-only `_is_ambiguous_code` check).

Both layers run on every real case (see `map_payload_to_case`). The first exists because
`guardrails.py`'s literal-string rules were written against the synthetic vocabulary and
were not (yet) rewritten to consume bucket values; the second exists specifically to fix
LLM-routing, which structurally never fired on real traffic before it was added (every
real `error_reason` fell through the small first table's synthetic-prefix match).

---

## 5. Audit & log schemas

**`logs/audit_log.csv`** (`run_batch.AUDIT_COLUMNS`, 25 columns) — exclusively
`pipeline/run_batch.py`'s frozen 280-row synthetic-batch output, read only by
`pipeline/validate_audit_log.py` (which asserts an exact row count against that batch).
**Never** touched by the live webhook path.

```
case_id, timestamp, decline_code, amount, tree_model_score, routing_rationale,
tree_model_top_features, routed_to_llm, llm_recommended_action, llm_confidence,
llm_reasoning_summary, llm_schema_valid, guardrail_flags, proposed_action, final_action,
guardrail_overrode, override_rule, requires_human_review, model_version, pipeline_version,
baseline_final_action, baseline_guardrail_overrode, baseline_override_rule,
pipeline_retried, baseline_retried
```

**`logs/webhook_audit_log.csv`** (`run_case.WEBHOOK_AUDIT_COLUMNS`, 41 columns) — the
live pipeline-decision log: the 25 columns above, in the same order (so nothing reading
them by name breaks), plus 16 appended columns, populated only by whichever row-type
actually sets them (everything else is `None`):

```
decline_code_bucket, decline_code_is_ambiguous, customer_key, customer_history_source,
execution_status, execution_mechanism, execution_detail, execution_timestamp, source,
ptp_offer_decision, ptp_trigger_category, ptp_offer_reason, retry_attempt_number,
ltv_tier, payment_rail, cumulative_retries_this_txn
```

`source` is `"real_webhook"` or `"manual_test"` (dashboard-triggered cases always start
`case_id` with `manual_test_`, so they can never be mistaken for real customer events).

**`logs/webhook_log.csv`** (6 columns) — transport-layer log of **every** POST to
`/webhook/razorpay`, including ones that never reach the pipeline (signature rejections,
malformed JSON): `timestamp, event_type, signature_valid, case_id, outcome, error_detail`.

**`logs/promise_log.csv`** (14 columns) — webhook_receiver's own secondary audit trail for
`/api/promise-reply` calls (lacks `guardrail_status`/`payment_link_id`; the `promises`
SQLite table is the actual source of truth for a promise's lifecycle):
```
timestamp, case_id, customer_id, promise_id, message, outcome, extracted_date,
extraction_confidence, ambiguous, clarification_needed, model_version,
clarification_round, status, fallback_mechanism
```

**`logs/ptp_outcome_log.csv`** (9 columns) — every honor/break/late-recovery/ignored
transition from `pipeline/ptp_outcomes.py`:
```
timestamp, event_type, promise_id, case_id, customer_id, payment_link_id, extracted_date,
trigger, reason
```

**`logs/pending_retries.csv`** (4 columns) — `retry_scheduled` cases with no real
scheduler behind them: `case_id, customer_key, scheduled_for, logged_at`.

---

## 6. Discrepancies & known gaps

These are places where the code's actual behavior diverges from what a name, docstring,
or the obvious reading of a comment implies. Documented here rather than silently
described as working as named.

- **`npci_peak_window_check` (PTP guardrail) cannot fire in the current data flow.**
  It reads `extraction.get("action_scheduled_for")`, but `schema.PromiseDateExtraction`
  has no such field — the LLM's promise-date extraction only ever produces
  `extracted_date` (a date, not a timestamp), `confidence`, `ambiguous`,
  `clarification_needed`. Nothing in `webhook_receiver.py` ever attaches an
  `action_scheduled_for` onto the extraction dict before calling `apply_ptp_guardrails`.
  The rule, its `_shift_scheduled_time_outside_npci_peak` adjustment logic, and its
  `"adjusted"` guardrail-status branch are exercised by `pipeline/test_ptp_guardrails.py`
  directly (constructing the extraction dict by hand) but are dead code on the real
  `/api/promise-reply` path.
- **Even if it did fire, an `"adjusted"` verdict would never reach execution.**
  `webhook_receiver.api_promise_reply` only calls `run_case.run_promise_reschedule` when
  `guardrail_result["guardrail_status"] == "approved"`. `"adjusted"` is a distinct status
  value (not `"approved"`), so a promise the NPCI-peak rule "adjusted" would simply never
  be scheduled — it would sit unresolved, not scheduled at the adjusted time as the rule's
  own docstring implies.
- **`hard_decline_excluded` does not reliably fire on real Razorpay traffic**, per
  `README_WEBHOOK.md`'s own "Known limitations" section: it matches
  `case["decline_code"]` against 4 literal synthetic strings
  (`stolen_card`/`lost_card`/`restricted_card`/`invalid_account`), populated only via the
  small `RAZORPAY_REASON_TO_DECLINE_CODE` table (§4's first layer). A real hard-decline
  `error_reason` not in that table's ~20 entries falls to `"generic_decline"` and this
  rule silently never fires for it — even though `decline_code_mapper`'s bucket
  (§4's second layer) correctly classifies it as `CLEAR_HARD` for routing purposes.
  These two layers are not kept in sync by design; only the second was ever fixed for
  real-traffic ambiguity routing.
- **`cumulative_retries_this_txn` is a same-transaction proxy, not a true 30-day rolling
  network retry count.** Both `shap_extract.get_case_facts` and `guardrails.py`'s
  `NETWORK_RETRY_CAP_THIS_TXN` comment this explicitly: the data model has no
  customer/timestamp linkage across transactions, so this is honestly named as a proxy
  (this case's own retry count so far) rather than mislabeled as the cross-transaction
  figure `network_retry_cap_exceeded`'s name might suggest.
- **`retry_now` never actually retries anything via a Razorpay retry API** — no such API
  exists. It sends a Payment Link instead (see `execute_action.py`'s module docstring, and
  every `execution_detail` string, which states this explicitly rather than letting the
  substitution pass unnoticed).
- **`retry_scheduled` schedules nothing.** It appends a row to
  `logs/pending_retries.csv` and stops — there is no scheduler or cron job that ever reads
  that file and fires the retry. This is called out in the code as a known limitation, not
  a bug, but a reader of `final_action == "retry_scheduled"` in the audit log should not
  assume anything actually happens at `action_scheduled_for`.
- **`_resolve_fallback_schedule`'s "predictor" path never runs** in the current repo — it
  tries to `import retry_time_predictor`, a module that does not exist anywhere under
  `pipeline/`. Every PTP clarification-loop fallback in practice uses the fixed
  `fixed_default_24h` path, never a real per-customer pattern prediction, despite the
  function being written to prefer the predictor.
- **`pattern_conflict_check`** (a PTP guardrail comparing a promised date against a
  customer's historical day-of-month/time-of-day success pattern) is explicitly not
  implemented — no such per-customer pattern exists anywhere in `customer_history.py` or
  elsewhere. This is documented in `guardrails.py`'s own module comment, not silently missing.
- **`"restricted"` risk tier is a one-way door with no reset mechanism.**
  `customer_ptp_stats.compute_risk_tier` never transitions a restricted customer back to
  `watch`/`normal`, and the module has an explicit `TODO_RESTRICTED_RESET_REQUIRES_HUMAN_REVIEW`
  flag noting that no admin endpoint or reviewed dashboard action exists yet to lift it.
- **The dashboard has no authentication**, and neither do the `/api/*` endpoints or the
  raw log files served through them — a deliberate change made at the user's request
  (per `webhook_receiver.py`'s own comment), not an oversight, but worth flagging plainly:
  everything at `https://razor-recover.heracle.fit` is publicly readable and triggerable.
- **The dashboards are not server-rendered in any sense.** `/dashboard` and
  `/conversations` return the static HTML file's bytes unmodified; every number, table
  row, and chart the operator sees is populated client-side via polling `fetch()` calls
  against the JSON APIs (5-second interval on the main dashboard, pausable). There is no
  templating engine and no data is ever injected server-side into the HTML.
