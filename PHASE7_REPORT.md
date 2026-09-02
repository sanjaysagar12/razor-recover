# Phase 7 Report — Confidence-Gate Template Fix + Batch Simulation

**This run used a MOCKED LLM adapter (deterministic fake, cycling through
actions). No real Claude API calls were made or spent.** All numbers below
come from `pipeline/run_batch.py` / `pipeline/validate_audit_log.py` run
against the mock.

---

## 1. The bug (plain language)

`confidence_gate.py`'s template action for cases NOT routed to the LLM
(~70% of the batch) was decided from tree-model score alone:
`score > band_high → retry_now`, else `no_retry_prompt_update`. It never
looked at the decline code.

`band_low`/`band_high` is a narrow **relative** percentile slice (42.5th–
57.5th) of this batch's own compressed logistic-regression scores (which
range roughly 0.06–0.71 on this dataset) — it is not an absolute "will
recover" cutoff. So "below band_low" was being read as "won't recover,"
when in reality a *soft*-decline case (insufficient funds, expired card)
sitting below that line still had a real, non-trivial chance of recovering
— the additive recovery-probability model backing the synthetic data gives
every `CLEAR_SOFT`-bucket case a baseline lift regardless of the other
signals the tree model happens to weight. Meanwhile a genuinely *hard*
decline (stolen/lost/restricted card, invalid account) correctly never
recovers regardless of score — but the old template didn't distinguish the
two cases; it wrote off both identically whenever the score alone was low.

## 2. The fix

Added decline-code awareness to the template, in `pipeline/confidence_gate.py`.

**Before:**
```python
if routed_to_llm:
    template_action = None
elif tree_probability > band_high:
    template_action = "retry_now"
else:
    template_action = "no_retry_prompt_update"
```

**After:**
```python
def _is_hard_decline(decline_code: str) -> bool:
    return decline_code in HARD_DECLINE_CODES  # imported from guardrails.py


def _template_action(tree_probability: float, decline_code: str, band_high: float) -> str:
    if _is_hard_decline(decline_code):
        return NO_RETRY_ACTION
    if tree_probability > band_high:
        return "retry_now"
    return "retry_scheduled"

...

template_action = None if routed_to_llm else _template_action(tree_probability, decline_code, band_high)
```

`HARD_DECLINE_CODES` and `NO_RETRY_ACTION` are imported from
`pipeline/guardrails.py` (the same "CLEAR_HARD" bucket guardrails already
uses) rather than redefined, so the two layers can't drift apart. The
routing band itself (`PROB_BAND_LOWER_PCTILE`/`PROB_BAND_UPPER_PCTILE`,
which cases get routed to the LLM at all) was **not** touched — only the
action assigned to already-non-routed cases changed. A non-hard decline
below `band_high` now gets `retry_scheduled` (a lower-urgency retry) instead
of being written off; a hard decline still gets `no_retry_prompt_update`
unconditionally, regardless of score, without relying on `guardrails.py` to
correct it downstream.

`tests/test_guardrails.py` (21 tests, including `tests/test_phase4.py`)
still passes unchanged — no test asserted the specific non-routed template
string, only routing behavior, which is untouched.

## 3. Leaked recoverable amount

Filtered to `routed_to_llm == False`, `would_recover == True`
(`outcome == 1`), `final_action` NOT IN `{retry_now, retry_scheduled,
prompt_alt_payment}`:

| | Before fix | After fix |
|---|---:|---:|
| Leaked amount | ₹17,833.87 | ₹3,119.34 |
| Leaked cases | 12 | 3 |

The residual ₹3,119.34 (2× `stolen_card`, 1× `invalid_account`) is
**expected, not a bug** — these are hard declines that `guardrails.py`'s
`hard_decline_excluded` rule correctly blocks from ever retrying,
regardless of what the ground-truth label says happened historically (label
noise / the 4% random flip in the synthetic generator). The fix closed
100% of the leak that was actually addressable at the template level
(₹14,714.53 across 9 soft-decline cases: `51_insufficient_funds`,
`insufficient_funds`, `expired_card_soft`, `generic_decline`).

## 4. Headline numbers, before vs. after

Both runs use the identical mocked LLM adapter (same deterministic action
cycle) so the only variable is the confidence-gate fix.

| Metric | Before fix | After fix |
|---|---:|---:|
| pipeline_gross_recovered | ₹103,136.48 | ₹115,769.44 |
| baseline_gross_recovered | ₹134,822.91 | ₹134,822.91 |
| pipeline_net_recovered | ₹101,766.48 | ₹114,159.44 |
| baseline_net_recovered | ₹132,872.91 | ₹132,872.91 |
| net_lift_absolute | -₹31,106.43 | -₹18,713.47 |
| net_lift_pct | -23.4% | -14.1% |
| pipeline_retry_attempts | 137 | 161 |
| baseline_retry_attempts | 195 | 195 |
| wasted_retries_avoided | 58 | 34 |

`baseline_gross_recovered`/`baseline_net_recovered`/`baseline_retry_attempts`
are unchanged, as expected — the naive baseline always proposes `retry_now`
for every case regardless of the tree model's template logic, so this fix
(which only touches the non-routed template) cannot affect it.
`wasted_retries_avoided` dropped (58→34) because 35 previously-skipped
cases now correctly retry — this is a deliberate trade of some avoided-cost
for recovered gross revenue, and the net figures show it was a good trade
(net improved by ₹12,393 even after the extra attempt cost).

## 5. Validation checks (after fix)

| Check | Status | Detail |
|---|---|---|
| row_count | PASS | 280 audit rows == 280 input rows |
| final_action_enum | PASS | every final_action in the fixed enum, no free text |
| routed_to_llm_ratio | PASS | 84/280 = 30.0%, within [15%, 50%] |
| guardrail_fired | PASS | 24 pipeline guardrail overrides |
| **net_recovered** | **FAIL** | pipeline_net (₹114,159.44) is not > baseline_net (₹132,872.91) |
| retry_attempts | PASS | pipeline (161) < baseline (195) |
| wasted_retries_sanity | PASS | 85 baseline cases blocked by guardrails (non-zero) |
| gross_recovered (info only, not asserted) | — | pipeline ₹115,769.44 vs baseline ₹134,822.91 |

**6/7 checks pass.** `net_recovered` still fails, down from a ₹31.1k gap to
an ₹18.7k gap.

## 6. Recommendation

**The fix is correct and worth keeping regardless of the outcome below** —
it closed a genuine decision-quality bug (₹14.7k in soft-decline cases
written off purely from a misread relative score threshold), independent
of anything the LLM layer does.

**On whether to spend real API calls now:** the confidence-gate fix only
touched the 70% of cases that never reach the LLM. The remaining ₹18.7k
gap is now dominated by the 30% of cases *routed to the LLM*, where this
dry run used a mocked adapter that cycles through actions essentially at
random — not a reasoning-based choice. That's exactly the part a real
Claude call is supposed to improve, and it's untested by this dry run. So a
real run is likely to move the number further in the pipeline's favor and
is worth trying next.

However, don't expect it to fully close the gap on its own, and here's the
structural reason why: `baseline_retried` is provably a superset of
`pipeline_retried` for any policy that ever declines a retry (the naive
policy retries everything guardrails allow; a selective pipeline retries a
subset of that by design). With `COST_PER_RETRY_ATTEMPT = 10` — a small
placeholder relative to typical case amounts (₹199–₹9,999) — the avoided-
attempt savings from being selective are dwarfed by any recoverable revenue
missed on the routed slice. Two independent levers exist if the real run
still falls short:
1. **Real LLM quality** on the 84 routed cases (untested here, worth trying
   first since it's free of further code changes).
2. **`COST_PER_RETRY_ATTEMPT` realism** — ₹10 may understate the true
   cost of a retry attempt (issuer/network penalty risk, chargeback
   exposure from retrying already-doomed cases, customer friction). This is
   a business-input decision, not something to tune blindly to force a
   pass — but if real-LLM numbers still fail net_recovered, it's the next
   thing worth investigating with real cost data rather than further model
   changes.

**Bottom line: worth spending the real API run next**, with the
expectation that it narrows but may not fully close the gap, and that
`COST_PER_RETRY_ATTEMPT` is the more likely remaining lever if it doesn't.

---

## Update — 2026-08-27: Real Gemini run (LLM_PROVIDER=gemini)

**This run replaced the mocked adapter with the real
`pipeline/llm_layer.py` call for all `routed_to_llm` cases** — 84 real
`gemini:gemini-3.1-flash-lite` calls (30.0% of 280 cases, matching the
dry run's routing ratio, confirming `confidence_gate.py`'s routing band was
correctly left untouched). No changes were made to `confidence_gate.py`,
`guardrails.py`, the routing band, or `COST_PER_RETRY_ATTEMPT` in this step
— this run isolates the LLM layer's real output quality only. Elapsed:
169.1s.

### Headline numbers: mocked vs. real

| Metric | Mocked run | Real run (Gemini) |
|---|---:|---:|
| pipeline_gross_recovered | ₹115,769.44 | ₹125,958.49 |
| baseline_gross_recovered | ₹134,822.91 | ₹134,822.91 |
| pipeline_net_recovered | ₹114,159.44 | ₹124,248.49 |
| baseline_net_recovered | ₹132,872.91 | ₹132,872.91 |
| net_lift_absolute | -₹18,713.47 | -₹8,624.42 |
| net_lift_pct | -14.1% | -6.5% |
| pipeline_retry_attempts | 161 | 171 |
| baseline_retry_attempts | 195 | 195 |
| wasted_retries_avoided | 34 | 24 |

`baseline_*` figures are unchanged, as expected — the naive baseline never
calls the LLM, so it's a pure control across all three runs (original,
mocked-fix, real). The real Gemini run recovered ₹10,189.05 more gross
revenue than the mocked run using only 10 more retry attempts, cutting the
net gap roughly in half (-14.1% → -6.5%). This is exactly the improvement
predicted in the prior section: real reasoning on the ambiguous 30% slice
outperforms the mocked adapter's near-random choices.

### Validation checks (real run)

| Check | Status | Detail |
|---|---|---|
| row_count | PASS | 280 audit rows == 280 input rows |
| final_action_enum | PASS | every final_action in the fixed enum, no free text |
| routed_to_llm_ratio | PASS | 84/280 = 30.0%, within [15%, 50%] |
| guardrail_fired | PASS | 22 pipeline guardrail overrides |
| **net_recovered** | **FAIL** | pipeline_net (₹124,248.49) is not > baseline_net (₹132,872.91) |
| retry_attempts | PASS | pipeline (171) < baseline (195) |
| wasted_retries_sanity | PASS | 85 baseline cases blocked by guardrails (non-zero) |
| gross_recovered (info only, not asserted) | — | pipeline ₹125,958.49 vs baseline ₹134,822.91 |

**6/7 checks pass.** `net_recovered` still fails. Gap: -₹8,624.42 (-6.5%),
down from -₹18,713.47 (-14.1%) on the mocked run and -₹31,106.43 (-23.4%)
on the pre-fix mocked run.

### LLM schema-validity rate

**84/84 (100%)** of real Gemini calls were schema-compliant on the first
attempt — zero fell back to the `requires_human_review=True` /
`llm_schema_valid=False` validation-failure path (see
`llm_layer.validate_llm_output`). No retries of `MAX_GENERATE_ATTEMPTS`
were consumed by schema failures.

### Sample reasoning summaries (3 random routed cases)

All three cite exactly the 5 SHAP features actually passed for that case,
with matching signs and rounded values, and none invent an outside factor
as a *ranked* driver — a good sign the model is reasoning over the given
evidence rather than confabulating. (One summary, `case_0221`, mentions
`peak_window_risk` from `case_facts` but explicitly labels it "unranked
context, not a SHAP-attributed driver" rather than treating it as a
ranked feature — correctly grounded, not a violation.)

- **case_0221** (`05_do_not_honor`, ₹2,552.06, score 0.4905) → `retry_scheduled`, confidence 0.65: weighs positive `historical_ptp_honor_rate` (+0.346) and `ltv_tier` (+0.279) against negative `amount` (-0.260), `customer_tenure_days` (-0.208), `issuer_bank_risk_tier` (-0.120); concludes the positive reliability signals outweigh the negatives. `guardrail_overrode: False`.
- **case_0142** (`05_do_not_honor`, ₹990.56, score 0.4286) → `retry_scheduled`, confidence 0.65: leads with strong positive `historical_ptp_honor_rate` (+0.551) against negative `issuer_bank_risk_tier` (-0.258), `ltv_tier` (-0.201), `customer_tenure_days` (-0.165); explicitly notes uncertainty ("missing data on the specific issuer bank response context") and picks a scheduled (not immediate) retry accordingly. `guardrail_overrode: False`.
- **case_0177** (`generic_decline`, ₹585.08, score 0.4423) → `escalate_human`, confidence 0.85: notes positive `historical_ptp_honor_rate` (+0.457) and `customer_tenure_days` (+0.141) but weighs them against negative `prior_retry_success_count` (-0.339), `ltv_tier` (-0.201), and `retry_attempt_number` (-0.171) — correctly reasons that a 4th retry attempt with a penalized prior-retry-success signal points to a "likely terminal issue," escalating rather than retrying blind. `guardrail_overrode: False`.

### Status: net_recovered still fails — flagged as open, not adjusted

Per instruction, `COST_PER_RETRY_ATTEMPT` (still `10.0`) was **not**
changed to force a pass. The real run closed roughly 55% of the mocked
run's net gap (-14.1% → -6.5%) purely from LLM reasoning quality, with no
further code changes. The remaining -₹8,624.42 gap is now small relative
to the total batch (₹347,524) and plausibly closeable by either:

1. A stronger/larger model on the same 84 cases (untested — this run used
   `gemini-3.1-flash-lite`, the lite/fast tier), or
2. A revised `COST_PER_RETRY_ATTEMPT` reflecting real per-attempt cost data
   (still an open business-input decision, not something to set
   speculatively here).

This is now a genuinely close call rather than a structural mismatch — the
recommendation is to bring a real cost-per-attempt figure (or decide
whether a stronger model tier is worth testing) before the next iteration,
not to keep spending API calls against the same open assumption.
