# PTP_FLOW.md — Promise-to-Pay Conversation Flow

This document traces the promise-to-pay (PTP) subsystem end to end, as it exists in code
today. It is a deep-dive companion to `ARCHITECTURE.md` §3/§4/§6 — read that first for how
PTP sits alongside the main decline-recovery pipeline. Everything here is drawn directly
from `pipeline/ptp_trigger.py`, `pipeline/promise_store.py`, `pipeline/guardrails.py`
(the `PTP_GUARDRAIL_RULES` section), `pipeline/llm_layer.py`
(`extract_promise_date`), `pipeline/execute_action.py`
(`execute_promise_reschedule`), `pipeline/ptp_outcomes.py`,
`pipeline/customer_ptp_stats.py`, and `webhook_receiver.py`'s `/api/promise-reply`
handler and its supporting helpers.

---

## 1. What PTP is, in one paragraph

A promise-to-pay is a customer's free-text commitment to pay by a specific date, collected
outside the automatic-retry flow. Not every failed payment is offered this: an
eligibility gate decides whether asking the customer is even appropriate for a given case.
When it is, the customer's reply is stored verbatim, then parsed by an LLM into a
structured date. A low-confidence or unparseable reply triggers a capped clarification
loop rather than a guess. A clean extraction is run through its own guardrail pass
(distinct from the recovery-action guardrails) that can reject it, ask for clarification,
or approve it. An approved date results in a real Razorpay payment link that expires at
end-of-day on the promised date. Later payment webhooks resolve the promise as honored or
broken; a background sweep also breaks any promise whose date has silently passed with no
payment. Every resolution feeds a per-customer risk-tier ladder (`normal → watch →
restricted`) that loops back into both the main pipeline's routing and its guardrails, and
into whether this same customer is offered a PTP conversation again.

---

## 2. Actors and entry points

PTP touches three separate HTTP-reachable entry points and one background thread:

1. **`POST /api/promise-reply`** — the customer (or an operator, via the dashboard's
   case-detail modal or the Customer Conversations page) submits a free-text reply for a
   `case_id`. This is where nearly all of the logic in this document runs.
2. **`POST /webhook/razorpay`, event `payment.captured` / `payment_link.paid`** — Razorpay
   tells the system a payment link was paid. Routed to `ptp_outcomes.handle_payment_captured`.
3. **`POST /webhook/razorpay`, event `payment.failed`** — routed alongside (not instead
   of) the main recovery pipeline to `ptp_outcomes.handle_payment_failed`, which is purely
   observational.
4. **Background daemon thread** (`ptp_outcomes.start_background_expiry_checker`, 5-minute
   interval, started once at process startup) and its manual twin,
   **`POST /api/ptp/check-expired`** — sweep for promises whose date has passed with no
   payment.

Additionally, every `payment.failed`/`subscription.pending`/`subscription.halted` case
running through the *main* recovery pipeline calls `ptp_trigger.should_offer_ptp` as a
**transparency-only** step (§3 of `ARCHITECTURE.md`) — it's logged onto that case's audit
row but never itself triggers a reply prompt or changes `final_action`. The actual
gate that can reject a reply is the one described in §4 below, run again at reply time.

---

## 3. Flow diagram

```mermaid
flowchart TD
    Start[POST /api/promise-reply\ncase_id, customer_id, message] --> Validate{case_id, customer_id,\nmessage all non-empty?}
    Validate -- no --> V400[400 bad_request]
    Validate -- yes --> IsContinuation{Latest promise for this\ncase_id already STATUS_CLARIFYING?}

    IsContinuation -- yes, round 2/3 of the\nsame conversation --> CreatePromise
    IsContinuation -- no, first reply --> Gate[ptp_trigger.should_offer_ptp\nreconstructed from original scoring-time case facts]

    Gate -- offer_ptp=False --> Reject[200 rejected\naudit row: ptp_reply_rejected]
    Gate -- offer_ptp=True --> CreatePromise[promise_store.create_promise\nraw reply saved BEFORE anything else]

    CreatePromise --> Extract[llm_layer.extract_promise_date\ncase_context.today = IST today]
    Extract --> CleanCheck{extracted_date present\nAND confidence >= 0.6\nAND not ambiguous?}

    CleanCheck -- no --> ClarLoop{prior clarification_round\n< MAX_CLARIFICATION_ROUNDS (2)?}
    ClarLoop -- yes --> AskAgain[round += 1, STATUS_CLARIFYING\nreturn follow_up_message to customer]
    ClarLoop -- no, cap reached --> Fallback[STATUS_FALLBACK, OUTCOME_NO_REPLY\nfixed_default_24h schedule\nno payment link created]

    CleanCheck -- yes --> PTPGuardrails[guardrails.apply_ptp_guardrails\nwindow_cap / past_date / npci_peak* / low_confidence]
    PTPGuardrails -->|approved| Reschedule[execute_action.execute_promise_reschedule\nnew Razorpay payment link,\nexpire_by = 23:59:59 IST on promised date]
    PTPGuardrails -->|rejected_window_cap| HumanReview[STATUS_REQUIRES_HUMAN_REVIEW]
    PTPGuardrails -->|rejected_past_date| ReAsk[routed_to_clarification\n— not currently looped back automatically]
    PTPGuardrails -->|pending_clarification| ReAsk

    Reschedule -- success --> Scheduled[promise_store: payment_link_id set,\nSTATUS_SCHEDULED]
    Reschedule -- failure --> RescheduleFailed[OUTCOME_RESCHEDULE_FAILED\nno payment link, no auto-retry]

    Scheduled --> WaitForPayment[Waiting on Razorpay webhooks]

    subgraph Resolution["Resolution — separate webhook path"]
        PayCaptured[payment.captured / payment_link.paid] --> Match[ptp_outcomes.find_any_promise\nmatch by notes.promise_id, else payment_link_id]
        Match -- matches pending --> Honored[outcome=honored, resolved_at=now]
        Match -- matches already-broken --> LateRecovery[late_recovery_at=now\noutcome STAYS broken]
        Match -- no match / already resolved --> NoOp[logged no-op, idempotent]

        Sweep[Background sweep, every 5 min\nor POST /api/ptp/check-expired] --> ExpiredCheck{outcome=pending AND\npayment_link_id set AND\nextracted_date < today IST?}
        ExpiredCheck -- yes --> Broken[outcome=broken, resolved_at=now]
    end

    Honored --> Stats[customer_ptp_stats.record_promise_resolution\nrecompute honor rate + risk tier]
    Broken --> Stats
    Stats --> RiskTier[current_risk_tier: normal / watch / restricted]
    RiskTier -.->|watch: always routes\nto LLM in main pipeline| MainPipeline[(confidence_gate.route_case)]
    RiskTier -.->|restricted: forces\nescalate_human, and vetoes\nfuture should_offer_ptp| MainGuardrails[(guardrails.apply_guardrails\n+ ptp_trigger gate)]
```

---

## 4. Step-by-step walkthrough

### 4.1 Eligibility gate — should this case even be offered PTP?

`ptp_trigger.should_offer_ptp(case)` (`pipeline/ptp_trigger.py`) is evaluated twice in the
system, on two different case-fact snapshots:

- **At scoring time**, inside `run_case.run_recovery_case`, against the case facts the
  main pipeline just built — logged onto the audit row (`ptp_offer_decision`,
  `ptp_trigger_category`, `ptp_offer_reason`) but purely informational.
- **At reply time**, inside `webhook_receiver.api_promise_reply` — this is the one that
  can actually reject a reply. It's re-run because `has_open_promise` and
  `current_risk_tier` can genuinely change between scoring time and reply time.

First-match-wins order (a case can satisfy more than one condition; only the most specific
fires):

| # | Category | Condition | Result |
|---|---|---|---|
| 1 | `restricted_tier` | `customer_ptp_stats.get_risk_tier(customer_id) == "restricted"` | **veto** |
| 2 | `hard_decline` | `decline_code_mapper` bucket is `CLEAR_HARD` | **veto** |
| 3 | `open_promise_exists` | `promise_store.has_open_promise(customer_id)` is true | **veto** |
| 4 | `high_ltv_first_failure` | `ltv_tier=="high"` and `retry_attempt_number<=1` | offer |
| 5 | `insufficient_funds_code` | `decline_code` ∈ `{insufficient_funds, 51_insufficient_funds}` | offer |
| 6 | `approaching_retry_cap` | one attempt before the NPCI/network retry cap | offer |
| 7 | `first_failure_awaiting_auto_retry` | `retry_attempt_number<=1`, nothing else matched | **veto** (let the silent auto-retry run first) |
| 8 | `retry_failed_once` | fallback — anything past a first failure | offer |

At reply time, the case facts needed for this check (`decline_code`, `decline_code_bucket`,
`retry_attempt_number`, `ltv_tier`, `payment_rail`, `cumulative_retries_this_txn`) are
**reconstructed from that `case_id`'s original scoring row** in `webhook_audit_log.csv`
(`webhook_receiver._case_facts_for_ptp_gate`, reading `.iloc[0]` — the first row ever
written for the case, never a later reschedule/rejection row). If no audit row exists at
all (e.g. the log was reset, or a test hits `/api/promise-reply` directly), the gate falls
back to `retry_attempt_number=2` specifically so the missing-data default doesn't
masquerade as a genuine first failure — `has_open_promise`/`current_risk_tier` still run
unconditionally regardless.

**Important carve-out:** this gate only runs on a case's **first** reply. If the latest
promise for this `case_id` is already `STATUS_CLARIFYING` (i.e. this reply is round 2 or 3
of an ongoing clarification loop), the gate is skipped entirely — `has_open_promise` can't
distinguish "this case's own still-open clarification round" from "an unrelated open
promise," so re-running it mid-loop would reject a case's own continuing conversation
against itself.

A rejection returns `200` with `status: "rejected"` and appends an audit row
(`final_action="ptp_reply_rejected"`, `execution_status="not_applicable"`) — a rejected
attempt is still logged as an event, not silently dropped.

### 4.2 Reply intake — durable storage before anything else

Once past the gate, `promise_store.create_promise(case_id, customer_id, message)` inserts
a new row with a fresh UUID `promise_id`, `outcome="pending"`, before extraction is even
attempted. This ordering is deliberate: if the LLM call in the next step fails outright,
the customer's raw reply is never lost — it just degrades to an ambiguous outcome. Every
reply gets its **own** row (not an update to a running per-case row), so a case with three
back-and-forth exchanges has three `promises` rows, most-recent findable via
`get_latest_promise_for_case`.

### 4.3 Date extraction

`llm_layer.extract_promise_date(message, case_context)` — `case_context` must include
`today` (IST-local date, matching the convention the rest of the system uses for
day-of-week/time-of-day; `webhook_receiver._promise_date_case_context` builds this,
opportunistically adding `amount`/`decline_code` from the case's audit row for grounding,
best-effort and never blocking on a lookup miss). The LLM (same `LLM_PROVIDER`
Claude/Gemini adapter as the recovery-action decision, different system prompt and tool
schema) returns `{extracted_date, confidence, ambiguous, clarification_needed}`.
`validate_promise_date_output` enforces `YYYY-MM-DD` formatting and a hard postcondition:
an `ambiguous=True` response that still filled in a date has that date **discarded**
(logged, not trusted) rather than risking an unimplied guess. One retry on schema failure;
unconditional fallback to `ambiguous=True, confidence=0.0` if both attempts fail — there is
always a clarification question to fall back on, never an unhandled exception.

A **clean extraction** requires all three: `extracted_date is not None`, `confidence >=
guardrails.PTP_CONFIDENCE_FLOOR` (0.6), and `not ambiguous`. Anything short of that goes to
the clarification loop instead — it never reaches the PTP guardrail pass at all.

### 4.4 Clarification loop (capped, then automatic fallback)

For a non-clean extraction, `webhook_receiver._handle_promise_clarification` reads the
**prior** promise row for this `case_id` (excluding the one just inserted in 4.2) to find
the running `clarification_round`:

- If `previous_round < MAX_CLARIFICATION_ROUNDS` (2): increment the round, set
  `STATUS_CLARIFYING`, and return a follow-up question to send the customer
  (`extraction["clarification_needed"]`, or a generic default). The customer's next reply
  re-enters this whole flow at 4.1, skipping the eligibility gate per the carve-out above.
- If the cap is already reached: stop asking. `_resolve_fallback_schedule` tries to
  `import retry_time_predictor` (a module that **does not exist anywhere in this repo** —
  see §6) and, failing that, falls back to a fixed `now + 24h` schedule
  (`fallback_mechanism="fixed_default_24h"`). The promise is marked `STATUS_FALLBACK` /
  `OUTCOME_NO_REPLY`. **No payment link is created for a fallback** — the 24h timestamp is
  returned to the caller/logged but nothing in `execute_action.py` acts on it.

Every round (clarifying or fallback) is appended to `logs/promise_log.csv`, including
`clarification_round`/`status`/`fallback_mechanism` — this file is the "ask-again-or-give-up
history" for a case, distinct from the `promises` SQLite table's current-state row.

### 4.5 PTP guardrails — validating a customer-supplied date before scheduling anything

Only a clean extraction reaches `guardrails.apply_ptp_guardrails(case_context, extraction)`.
This is a **separate rule list** from the main `GUARDRAIL_RULES` (no `recommended_action`
to override here), evaluated in this order, every rule always run, first rule whose
override sets `guardrail_status` wins:

| # | Rule | Fires when | Result |
|---|---|---|---|
| 1 | `window_cap_check` | promised date more than 30 days out | `rejected_window_cap`, `final_action=no_retry_prompt_update`, **`requires_human_review=True`** — routes to `STATUS_REQUIRES_HUMAN_REVIEW`, no reschedule attempted |
| 2 | `past_date_check` | promised date before `case["today"]` | `rejected_past_date`, `routed_to_clarification=True` (more likely a parsing/timezone slip than a real problem) |
| 3 | `npci_peak_window_check` | `payment_rail=="upi_autopay"` and a scheduled timestamp falls in `[10:00,13:00)` IST | `adjusted`, computes a shifted `adjusted_date` — **structurally dead in the current data flow, see §6** |
| 4 | `low_confidence_gate` | `confidence < 0.6` or `ambiguous=True` | `pending_clarification`, `routed_to_clarification=True` — redundant in practice since 4.3 already filters this before guardrails even run |

Only `guardrail_status == "approved"` (no rule fired) proceeds to execution. Every other
verdict is written back onto the promise row via `promise_store.update_promise_guardrail`,
and `requires_human_review=True` (from `window_cap_check`) additionally moves the promise
to `STATUS_REQUIRES_HUMAN_REVIEW` — otherwise the reply simply sits with its
`guardrail_status` visible, no execution attempted, no automatic re-prompt.

### 4.6 Execution — creating the real payment link

`run_case.run_promise_reschedule` → `execute_action.execute_promise_reschedule(case,
promise, guardrail_result)`. This refuses to run (raises `ValueError`) unless
`guardrail_status == "approved"` — a second, defensive check against a caller-side routing
bug, since it must never silently schedule a link for something guardrails didn't approve.

It creates a Razorpay Payment Link (`client.payment_link.create`) — there is no dedicated
"reschedule a promise" API, same substitution pattern as `retry_now` in the main pipeline
— with:
- `reference_id = promise_id` (not `case_id:promise_id`; a UUID4 alone stays under
  Razorpay's 40-char cap, which the combined string could exceed).
- `expire_by` = 23:59:59 **IST** on the promised date (`_date_to_ist_eod_epoch`) — the
  link stays valid through the whole promised day in the customer's own calendar, not the
  server's UTC one.
- `notes = {"promise_id": promise_id}` — this is what lets `ptp_outcomes.find_open_promise`
  reliably match a later `payment.captured`/`payment_link.paid` webhook back to this exact
  promise, more robust than parsing `payment_link_id` out of payloads whose shape varies
  by event type.

On success: `promise_store.update_promise_payment_link` (sets `payment_link_id`, `outcome`
stays `"pending"` — a later webhook resolves it) and `STATUS_SCHEDULED`. On any SDK/network
failure: caught, returned as `execution_status="failed"`, and
`promise_store.mark_promise_reschedule_failed` sets `outcome="reschedule_failed"` —
**never** a false-positive scheduled state for a failed API call. Either way, a row is
appended to `logs/webhook_audit_log.csv` (`proposed_action="reschedule_to_<date>"`).

### 4.7 Resolution — honor, break, or late recovery

`pipeline/ptp_outcomes.py` owns everything downstream of a scheduled promise:

- **`handle_payment_captured`** (`payment.captured`/`payment_link.paid`) matches via
  `notes.promise_id` first, `payment_link_id` as fallback:
  - Matches a `pending` promise → `mark_promise_honored` (`outcome="honored"`,
    `resolved_at=now`) + `customer_ptp_stats.record_promise_resolution(honored=True)`.
  - Matches an already-`broken` promise → **late recovery**: `late_recovery_at` is set,
    but `outcome` deliberately **stays** `"broken"` — the promise itself was broken; money
    arriving late is a separate fact, not counted as a second resolution in
    `customer_ptp_stats` (it was already counted broken once).
  - Matches anything else (already `honored`, `no_reply`, `reschedule_failed`), or no
    match at all → logged no-op. Webhook redelivery is expected here and must be idempotent.
- **`handle_payment_failed`** — observational only. **Never** marks a promise broken (the
  customer may retry the same payment link before the deadline); only the deadline sweep
  can do that.
- **`check_expired_promises`** (5-minute background thread, or `POST
  /api/ptp/check-expired` on demand) — the **only** path that marks a promise `broken`:
  `outcome="pending"` AND `payment_link_id IS NOT NULL` AND `extracted_date < today`
  (IST). The `payment_link_id IS NOT NULL` condition matters: a promise a guardrail
  rejected, or one still mid-clarification, never got a link and must never be reported as
  a broken promise-to-pay, since no payment was ever scheduled for it to break.

Every transition (`pending_to_honored`, `pending_to_broken`, `late_recovery_after_broken`,
`webhook_ignored_non_pending`) is appended to `logs/ptp_outcome_log.csv` — deliberately
**not** `logs/audit_log.csv` (the frozen synthetic-batch file).

### 4.8 Feedback loop — the risk-tier ladder

Every resolution (honored or broken — never a late recovery, which is not a second
resolution) calls `customer_ptp_stats.record_promise_resolution(customer_id, honored)`,
which upserts `customer_ptp_stats` and recomputes `current_risk_tier` via the pure
function `compute_risk_tier`:

- **`restricted` never auto-clears.** No promise outcome, however good, lifts it — an
  explicit `TODO_RESTRICTED_RESET_REQUIRES_HUMAN_REVIEW` flag in the code notes that no
  admin endpoint exists yet to do this.
- **`normal → watch`**: `historical_ptp_honor_rate < 0.5` AND `promises_made >= 2`.
- **`normal → restricted`** (skipping watch as a separate step): the above condition
  coincides with the two most recent resolved promises both being broken — with exactly 2
  total promises, a rate below 0.5 is only reachable if both broke, so this isn't a
  shortcut, it's the same fact observed once.
- **`watch → restricted`**: a second **consecutive** broken promise (not just the rate
  dipping further).
- **`watch → normal`**: only when the tier coming into this call was already `watch` and
  the most recent promise was honored.

This tier then feeds back into **two other subsystems**, closing the loop:
- `confidence_gate.route_case` — `risk_tier=="watch"` always routes the customer's *next*
  decline case to the LLM layer, regardless of tree-model score.
- `guardrails.apply_guardrails` — `customer_risk_restricted` is the **first** rule
  evaluated, forcing `final_action="escalate_human"` for any case belonging to a
  restricted customer, ahead of even the hard-decline rule.
- `ptp_trigger.should_offer_ptp` — `restricted_tier` is checked first there too, vetoing
  any future PTP conversation for that customer entirely.

---

## 5. Data model

### `promises` table (SQLite, `data/customer_history.db`)

One row per **customer reply** (keyed by generated `promise_id`, not `case_id` — a case
with several exchanges has several rows):

| Column | Notes |
|---|---|
| `promise_id` | TEXT PK, UUID4 |
| `case_id` | TEXT NOT NULL |
| `customer_id` | TEXT — raw id supplied at `/api/promise-reply` intake |
| `raw_customer_reply` | TEXT NOT NULL — stored verbatim, before any processing |
| `extracted_date` | TEXT, `YYYY-MM-DD`, NULL until extraction runs |
| `extraction_confidence` | REAL |
| `guardrail_status` | TEXT — `apply_ptp_guardrails`' verdict |
| `payment_link_id` | TEXT — set only on a successful reschedule |
| `outcome` | `pending` / `ambiguous` / `no_reply` / `reschedule_failed` / `honored` / `broken` |
| `created_at` | TEXT NOT NULL |
| `resolved_at` | TEXT — set only by honor/break |
| `clarification_round` | INTEGER, default 0 |
| `status` | `pending` / `clarifying` / `scheduled` / `fallback` / `requires_human_review` — a **separate lifecycle axis from `outcome`** |
| `late_recovery_at` | TEXT — set on a late-recovery match, `outcome` unchanged |

`outcome` and `status` are two independent axes, and this is a common source of confusion:
`outcome` answers "how did the underlying debt resolve" (Phase 9-16's question); `status`
answers "where is this reply in the ask-again-or-give-up loop" (Phase 15's question). A
promise can be `outcome="pending"` and `status="scheduled"` simultaneously — scheduled and
awaiting a real-world payment.

### `customer_ptp_stats` table

One row per `customer_id`, upserted only on a resolution (never on pending/ambiguous):

| Column | Notes |
|---|---|
| `customer_id` | TEXT PK |
| `promises_made` | INTEGER — total resolved promises |
| `promises_honored` | INTEGER |
| `historical_ptp_honor_rate` | REAL — `promises_honored / promises_made` |
| `current_risk_tier` | `normal` / `watch` / `restricted` |
| `created_at` / `updated_at` | TEXT |

---

## 6. Known gaps specific to PTP

- **`npci_peak_window_check` cannot fire today.** It reads
  `extraction.get("action_scheduled_for")`, but `schema.PromiseDateExtraction` has no such
  field — the LLM's date extraction only ever produces a *date*, not a timestamp, and
  nothing in `webhook_receiver.py` attaches one before calling `apply_ptp_guardrails`. The
  rule and its `"adjusted"` verdict are exercised only by `test_ptp_guardrails.py`
  constructing the extraction dict by hand. Even if it did fire, `"adjusted"` is not
  `"approved"`, so `api_promise_reply` would never call `run_promise_reschedule` for it —
  the promise would simply sit unresolved rather than being scheduled at the adjusted time.
- **`pattern_conflict_check`** (comparing a promised date against a customer's historical
  day-of-month/time-of-day success pattern) is explicitly unimplemented — no such
  per-customer pattern exists anywhere in `customer_history.py`. Documented in
  `guardrails.py` as a deliberate omission, not a gap to silently work around.
- **The clarification-loop fallback never uses a real predictor.**
  `_resolve_fallback_schedule` tries `import retry_time_predictor`; no such module exists
  under `pipeline/` in this repo, so every cap-reached fallback uses the fixed 24-hour
  default, despite the code being written to prefer a smarter prediction.
  **No payment link is ever created for a fallback** — it's a logged timestamp only.
- **`rejected_past_date` and `pending_clarification` set `routed_to_clarification=True`,
  but nothing in `webhook_receiver.py` currently automates re-asking the customer for
  these two verdicts.** The flag is written onto the guardrail result and would need an
  explicit caller-side branch to actually loop back into 4.4's clarification flow; today
  it's visible in the audit trail but the reply is left in whatever `status` it already
  had (`STATUS_PENDING`, since only `window_cap_check`'s `requires_human_review` and a
  successful reschedule ever move `status` forward from `api_promise_reply`).
- **`restricted` risk tier has no reset path.** Once a customer reaches it, no code path
  — automatic or admin — moves them back to `watch`/`normal`. This is called out as an
  intentional one-way door pending a future admin action, not a bug, but it means a
  customer who improves their payment behavior after being restricted has no way back into
  self-service PTP without a manual database edit.
- **`open_promise_exists` and the mid-clarification carve-out interact subtly.** The gate
  is skipped only when the *latest* promise for a `case_id` is `STATUS_CLARIFYING`. If a
  case's clarification loop already hit the cap and fell back (`STATUS_FALLBACK`), a
  further reply to the same case is treated as a **new first reply** and the gate runs
  again — `has_open_promise` will see that fallback promise's `outcome="no_reply"`, which
  counts as "still open" (§ `has_open_promise`'s docstring), and correctly reject it as
  `open_promise_exists` rather than starting a second parallel clarification thread.
