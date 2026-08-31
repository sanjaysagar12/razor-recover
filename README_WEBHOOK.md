# Webhook Receiver + Dashboard

Flask app (`webhook_receiver.py`) that receives Razorpay webhooks, verifies
their signature, maps the event into the pipeline's case schema, and runs it
through `confidence_gate -> shap_extract -> (conditionally) llm_layer ->
guardrails -> execute_action`, logging one row per case to
`logs/webhook_audit_log.csv`. It also serves a live dashboard for watching
that log and firing synthetic test cases without needing a real Razorpay
event.

## Log files -- which is which

- **`logs/audit_log.csv`** -- exclusively `pipeline/run_batch.py`'s frozen
  280-row synthetic training-batch output. Read by
  `pipeline/validate_audit_log.py`, which asserts its row count and merges it
  against that batch's ground-truth outcomes. **Never written to by the
  webhook receiver or the dashboard.**
- **`logs/webhook_audit_log.csv`** -- the live pipeline-decision log. Every
  real webhook case AND every dashboard-triggered synthetic case lands here
  (see the `source` column: `real_webhook` vs `manual_test`). Same 25-column
  schema as `audit_log.csv`, plus `decline_code_bucket`,
  `decline_code_is_ambiguous`, `customer_key`, `customer_history_source`,
  `execution_status`, `execution_mechanism`, `execution_detail`,
  `execution_timestamp`, `source`.
- **`logs/webhook_log.csv`** -- the transport-layer log: every POST to
  `/webhook/razorpay`, including ones that never reach the pipeline
  (signature rejections, malformed JSON, unhandled event types).
- **`logs/pending_retries.csv`** -- cases whose `final_action` was
  `retry_scheduled`, logged here since there's no scheduler/cron built yet
  (see Known limitations).
- **`logs/webhook_receiver.log`** -- full-detail step-by-step text log
  (request headers/body, every pipeline stage, execution result).

## Architecture note

`pipeline/shap_extract.py` was originally built to score cases already baked
into `data/train.csv` / `data/holdout.csv` (batch simulation, keyed by
`case_id` lookup). A live case is never one of those 280 rows, so
`shap_extract.score_new_case()` was added (pure addition, no existing
function touched) that scores an arbitrary case dict against the same cached
model + SHAP explainer.

`pipeline/run_case.py` holds the shared "run one case through the full
pipeline" logic (`run_recovery_case` / `run_recovered_case`) -- both the real
`/webhook/razorpay` route and the dashboard's `/api/trigger-test-case` call
these exact same functions, so nothing pipeline-related is duplicated between
the two entry points.

The confidence-gate probability band is normally refit per batch from that
batch's own score distribution; for single-case requests there's no batch to
refit against, so the band is fit once from the historical train+holdout
batch at process startup and reused for every case.

## Running

```bash
python webhook_receiver.py
```

Listens on `0.0.0.0:5555`. Requires `.env` (copy from `.env.example`) with at
least `RAZORPAY_WEBHOOK_SECRET` set — the process refuses to start without
it. On startup it warms up the pipeline (loads the model, fits the SHAP
background, computes the confidence-gate band), which takes a few seconds.

With the Cloudflare Tunnel already pointed at `localhost:5555`:
- Webhook endpoint: `https://razor-recover.heracle.fit/webhook/razorpay`
- Dashboard: `https://razor-recover.heracle.fit/dashboard`

## Dashboard

Open `https://razor-recover.heracle.fit/dashboard` (or
`http://localhost:5555/dashboard` locally). Same Flask process as the
webhook receiver — it shares the already-warmed-up model/SHAP context and
calls `pipeline/run_case.py` directly, so nothing is loaded twice.

**⚠️ No authentication.** The dashboard, every `/api/*` endpoint, and the
audit/webhook logs are all publicly readable at that URL, and
`/api/trigger-test-case` is publicly triggerable — including its ability to
create real (test-mode) Razorpay payment links via `execute_action.py`. This
was a deliberate choice (auth was removed on request); nothing about the
domain being public has changed. Put it behind Cloudflare Access, a VPN, or
re-add token auth (git history has the previous implementation) before
sharing the URL beyond a demo.

What it does:
- **Summary header** — total cases, routed-to-LLM count, guardrail-override
  count, human-review count, recovered vs. failed, execution-status
  breakdown. Polls `/api/summary` every 5s.
- **Audit Log / Webhook Log tables** — newest first, from
  `/api/audit-log` / `/api/webhook-log`, auto-refreshing every 5s (pause
  toggle available). Rows with `guardrail_overrode=true` or
  `requires_human_review=true` are highlighted. Client-side filters for
  `routed_to_llm`, `guardrail_overrode`, `execution_status`.
- **Preset buttons** — 5 hardcoded scenarios engineered against
  `pipeline/guardrails.py`'s actual `GUARDRAIL_RULES`, each POSTing to
  `/api/trigger-test-case` and showing the resulting `final_action` /
  `guardrail_overrode` in a toast. See "Preset scenarios" below for which
  ones are deterministic.
- **Custom trigger panel** — a JSON textarea for an arbitrary case payload,
  posted to the same endpoint, with the raw response shown back.

Every case triggered from the dashboard gets `case_id` prefixed
`manual_test_` and `source=manual_test` in the audit row — never
confusable with a real customer event. `/api/trigger-test-case` is
rate-limited to 20 requests/minute (in-memory, resets on restart) so it
can't be hammered into unbounded rows or unbounded real payment-link
creation.

### Preset scenarios

| Preset | Guardrail rule | Deterministic? |
|---|---|---|
| Hard decline override | `hard_decline_excluded` | Yes, in practice (see note) |
| NPCI retry cap exceeded | `npci_retry_cap_reached` | Yes |
| Peak execution window block | `npci_peak_window` | **No** — see note |
| Confidence floor breach | `requires_human_review_floor` | Yes (escalation-only — `guardrail_overrode` stays `False` by design) |
| Malformed LLM output | `requires_human_review_floor` (schema-validation fallback) | Yes (escalation-only, uses `run_case.MalformedTestAdapter` instead of a real LLM call) |

- **Hard decline override / NPCI retry cap** both force LLM routing so the
  guardrail actually gets a chance to override something (a hard-decline or
  NPCI-capped case that's *not* routed to the LLM already resolves to
  `no_retry_prompt_update` via `confidence_gate`'s own template logic, which
  shares `guardrails.HARD_DECLINE_CODES` — so the guardrail would fire but
  never *change* anything, and `guardrail_overrode` would read `False` even
  though the safety check worked correctly). Confirmed via live testing that
  the LLM's own proposal disagrees with the hard-decline case often enough
  for this to reliably demonstrate the override.
- **Peak execution window** only evaluates a timestamp the LLM itself
  chooses (`action_scheduled_for` on a `retry_scheduled` recommendation) —
  there's no case-input field that controls it. `pipeline/prompts.py` gives
  the LLM no current-date/timezone grounding, and in testing it consistently
  scheduled `09:00 UTC` (`14:30 IST` — outside the `[10:00,13:00)` window)
  regardless of prompt content. Click it more than once; it may take several
  tries, or may not land at all until the prompt is given real time context.
- **Confidence floor breach**'s `retry_attempt_number` must stay below `5`
  (`network_retry_cap_exceeded`'s threshold, which `cumulative_retries_this_txn`
  defaults from) or you'll see *two* rules fire at once and `override_rule`
  will show `network_retry_cap_exceeded` instead of isolating the
  confidence-floor path.

### Internal-shape vs. Razorpay-shape triggers

The dashboard's custom-trigger panel can POST two different body shapes, to
two different endpoints. Both end up running the exact same pipeline
(`confidence_gate -> shap_extract -> llm_layer -> guardrails ->
execute_action`), but they start from opposite ends of it:

| | `/api/trigger-test-case` ("internal shape") | `/api/trigger-webhook-shaped` ("Razorpay shape") |
|---|---|---|
| Body | Already a pipeline case dict — `{"event_type": ..., "case": {"decline_code": ..., "ltv_tier": ..., ...}}` | A full Razorpay webhook envelope — `{"entity": "event", "event": ..., "payload": {"payment": {"entity": {...}}}, ...}`, same shape `test_webhook_locally.py`'s `SAMPLE_PAYMENT_FAILED` uses |
| What builds the case dict | `webhook_receiver._build_manual_test_case()` — merges your `case` object directly onto `DEFAULT_TEST_CASE` | `webhook_receiver.map_payload_to_case()` — the SAME function the real `/webhook/razorpay` route uses |
| What you control | Every field on the case, exactly, as a literal value | Only what a real Razorpay webhook actually contains (`error_reason`, `amount` in paise, `method`, `customer_id`, ...) |
| Why it exists | Precise guardrail-rule targeting for the 5 presets below — several rules need an exact combination of field values that a real webhook can never carry (see next section) | Exercises the real mapping code path end-to-end, the same one production traffic runs through |

Both write `source=manual_test` and force the `manual_test_` case-id prefix,
same rate limit, same audit log — the only difference is which function
builds the case dict.

#### Where the conversion happens, and why some fields can't be set

`map_payload_to_case()` (`webhook_receiver.py`) is the one place a Razorpay
envelope becomes a case dict shaped like `/api/trigger-test-case`'s `case`
object. Every field on that case dict is filled in by one of three
mechanisms, and only the first one is something a webhook payload can
actually influence:

1. **Calculated from a field the webhook payload really has.** A lookup
   table or a unit conversion, nothing more.
   - `decline_code` ← `RAZORPAY_REASON_TO_DECLINE_CODE[error_reason]`
   - `decline_code_bucket` / `decline_code_is_ambiguous` ←
     `decline_code_mapper.map_razorpay_error_reason(error_reason)` — falls
     back to `AMBIGUOUS` for any `error_reason` not in that module's
     explicit list (real Razorpay error reasons are a much larger
     vocabulary than the model's training taxonomy covers)
   - `amount` ← `payload.payment.entity.amount / 100` (paise → rupees)
   - `payment_rail` ← `PAYMENT_METHOD_TO_RAIL[method]`
   - `retry_attempt_number` ← `payload.subscription.entity.paid_count`,
     defaulting to `1` if there's no subscription entity at all (true for
     every plain `payment.failed` event, which is most of them)

2. **Looked up from a database the webhook payload never mentions**,
   keyed only by `customer_id`. This is the one that surprises people —
   these fields exist *nowhere* in a Razorpay webhook; they're the
   system's own memory of that customer, built from every past event
   tied to that same ID:
   - `ltv_tier`, `historical_ptp_honor_rate`, `customer_tenure_days`,
     `prior_retry_success_count` ← `customer_history.get_or_create_customer(customer_key)`.
     A `customer_id` the system has never seen returns fixed neutral
     defaults (`medium` / `0.5` / `0` / `0`) — never anything you wrote in
     the webhook body, because there's nowhere in the body to write it.
   - `current_risk_tier` ← `customer_ptp_stats.get_risk_tier(customer_id)`,
     a separate database, same idea.

3. **A hardcoded constant.** No source at all, real webhook or not —
   every single case gets the same value:
   - `issuer_bank_risk_tier` = `"medium_risk"` (`DEFAULT_ISSUER_BANK_RISK_TIER`)
   - `amount_vs_historical_avg` = `1.0` (`DEFAULT_AMOUNT_VS_HISTORICAL_AVG`)
   - `hours_since_last_attempt` = `24.0` (`DEFAULT_HOURS_SINCE_LAST_ATTEMPT`)

`/api/trigger-test-case`'s `case` object skips all three mechanisms and
lets you write the *end result* directly — as if the lookup and the
calculation had already happened and landed on exactly the number you
wanted. That's the entire reason it exists alongside the Razorpay-shaped
endpoint, not instead of it.

**Worked example** — POSTing this to `/api/trigger-webhook-shaped`:

```json
{
  "event": "payment.failed",
  "payload": { "payment": { "entity": {
    "amount": 66242, "method": "upi", "customer_id": "cust_dashboard_example",
    "error_reason": "issuer_unavailable"
  } } }
}
```

does **not** produce `decline_code: "stolen_card"`, `payment_rail: "card"`,
`ltv_tier: "high"`, etc. no matter how those fields look in some
`/api/trigger-test-case` example — it produces `decline_code:
"issuer_unavailable"`, `decline_code_bucket: "AMBIGUOUS"` (that reason isn't
in `decline_code_mapper`'s table), `amount: 662.42`, `payment_rail:
"upi_autopay"`, and whatever `ltv_tier`/`historical_ptp_honor_rate`/etc.
`cust_dashboard_example` happens to have accumulated in
`data/customer_history.db` from past triggers (neutral defaults the first
time; drifting after that — unlike `/api/trigger-test-case`, which gives
the identical case every time).

#### Which presets can move to the Razorpay-shaped endpoint

| Preset | Reproducible via `/api/trigger-webhook-shaped`? | Why |
|---|---|---|
| Hard decline override | Yes | `hard_decline_excluded` checks `decline_code` (bucket 1, settable via `error_reason: "stolen_card"`), not `decline_code_bucket` — the guardrail outcome matches even though `decline_code_bucket` itself will read `AMBIGUOUS` instead of `CLEAR_HARD` |
| NPCI retry cap exceeded | Yes | `method: "upi"` + `payload.subscription.entity.paid_count: 4` reaches both `payment_rail="upi_autopay"` and `retry_attempt_number=4` |
| Peak execution window block | Yes | Already non-deterministic either way — depends only on the LLM's own chosen time, not on anything in the case shape |
| Confidence floor breach | **No** | Needs `ltv_tier`/`historical_ptp_honor_rate`/`customer_tenure_days`/`prior_retry_success_count` pinned to specific values — bucket 2 fields, only settable by accumulating real history for one `customer_id` over many actual events, not in one request |
| Malformed LLM output | **No** | Needs the internal-only `_force_malformed_llm` flag, which `map_payload_to_case()` structurally never sets (see `run_case.MalformedTestAdapter`'s own docstring) — there is no field in a Razorpay envelope for it |

## Running the local signature test

With `webhook_receiver.py` running in one terminal:

```bash
python test_webhook_locally.py
```

Posts a documented-shape `payment.failed` sample, an intentionally-corrupted
signature (expects `400`), a `subscription.charged` sample (recovered case),
and an unhandled event type (expects `status: "ignored"`).

## Razorpay Dashboard configuration checklist

- [ ] **Webhook URL**: `https://razor-recover.heracle.fit/webhook/razorpay`
- [ ] **Secret**: matches `RAZORPAY_WEBHOOK_SECRET` in `.env` exactly
- [ ] **Active events** to enable:
  - `payment.failed`
  - `subscription.pending`
  - `subscription.halted`
  - `subscription.charged`
- [ ] Confirm the account is in **Test Mode** while validating end-to-end
      (`RAZORPAY_KEY_ID` should start with `rzp_test_` — `pipeline/razorpay_client.py`
      already refuses to run against non-test keys)
- [ ] Confirm the Cloudflare Tunnel is up and routing to `localhost:5555`
- [ ] After saving, use the Razorpay dashboard's "Test Webhook" button (or
      trigger a real test-mode payment failure), then watch the row appear
      live at `https://razor-recover.heracle.fit/dashboard` (or check
      `logs/webhook_receiver.log` + `logs/webhook_audit_log.csv` directly)

## Known limitations / deviations from the task spec

- **Dashboard has no authentication** (see Dashboard section above) — a
  deliberate, explicit change; re-add auth before exposing this beyond a demo.
- **Decline-code mapping is best-effort.** The model was trained on a small
  synthetic decline-code taxonomy (`data/generate_synthetic.py`), not
  Razorpay's real `error_reason` vocabulary. `pipeline/decline_code_mapper.py`
  buckets the documented Razorpay error-reason list into the model's three
  training categories (`CLEAR_SOFT`/`CLEAR_HARD`/`AMBIGUOUS`); anything
  unrecognized defaults to `AMBIGUOUS`/`is_ambiguous=True` (routes to the LLM
  rather than being auto-actioned on a guess) and logs it.
- **`pipeline/guardrails.py`'s `hard_decline_excluded` rule matches
  `case["decline_code"]` against 4 literal synthetic strings**
  (`stolen_card`, `lost_card`, `restricted_card`, `invalid_account`), which
  real Razorpay `error_reason` values essentially never equal — real hard
  declines pass through `decline_code_mapper` into the `CLEAR_HARD` *bucket*,
  but the guardrail doesn't check the bucket, only the legacy literal
  `decline_code` string. Not yet fixed (flagged, out of the scope it was
  raised in).
- **Customer history is real now**, via `pipeline/customer_history.py`
  (SQLite, `data/customer_history.db`), keyed by `customer_id` (or
  `sub:<subscription_id>` fallback). A first-seen customer gets neutral
  defaults (`historical_ptp_honor_rate=0.5`, not `0.0`) and is logged as
  `customer_history_source=first_seen_defaults`, visibly distinct from
  `existing_history`. `data/customer_lookup.json` (the earlier, JSON-based
  approach) has been removed.
- `hours_since_last_attempt`, `issuer_bank_risk_tier`, and
  `amount_vs_historical_avg` still have no real source (not in any Razorpay
  webhook payload, not tracked anywhere) — fixed synthetic defaults, logged
  each time (see constants at the top of `webhook_receiver.py`).
- **`retry_now` doesn't call a real "retry" API** — Razorpay has none for
  merchant-triggered payment/subscription retries. `pipeline/execute_action.py`
  creates and sends a real Payment Link instead (the closest actual API that
  causes a retry to happen), and logs an explanatory note in
  `execution_detail` every time.
- **`retry_scheduled` has no scheduler** — logged to
  `logs/pending_retries.csv` only; nothing executes it later yet.
