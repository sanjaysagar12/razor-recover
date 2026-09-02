# Pitch Numbers -- Batch Simulation

Generated: 2026-09-02T08:35:19.007991+00:00

## a. Batch

- Cases in batch: 280
- Total amount at stake: Rs 347,524.00

## b. Gross Recovered

- Pipeline: Rs 125,958.49 (36.2% of total)
- Naive baseline (retry every case once, guardrailed): Rs 134,822.91 (38.8% of total)

Note: the pipeline's gross figure may be lower than or comparable to the naive baseline's -- it retries fewer, more carefully selected cases, so it is expected to leave some recoverable revenue on the table in exchange for far fewer wasted attempts. Gross recovered-$ is reported here for transparency but is not the headline number (see Net Recovered below).

## c. Retry Attempts

- Pipeline retry attempts: 170
- Naive baseline retry attempts: 195
- Wasted retries avoided (baseline - pipeline): 25
- Of the baseline's attempts, blocked into guaranteed-fail cases by compliance guardrails (hard declines, NPCI/network retry caps) had it not been guardrailed: 85

## d. Net Recovered (headline)

Assumes Rs 10.00 cost per retry attempt executed (see COST_PER_RETRY_ATTEMPT in pipeline/run_batch.py -- a placeholder assumption, not derived from data; tune as real per-attempt cost figures become available).

- Pipeline net recovered: Rs 124,258.49
- Naive baseline net recovered: Rs 132,872.91
- Net lift (absolute): Rs -8,614.42 (shortfall)
- Net lift (%): -6.5% (shortfall)

## e. Guardrail Overrides

Pipeline overrides, by rule:
- npci_retry_cap_reached: 15
- network_retry_cap_exceeded: 7

Naive baseline overrides, by rule (what a blind "retry everyone" policy would have executed into, had guardrails not blocked it):
- hard_decline_excluded: 63
- npci_retry_cap_reached: 15
- network_retry_cap_exceeded: 7

Compliance check: 0/280 cases executed a retry against a guardrail-blocked case (verified).

## f. Summary

Naive retry-everything recovers Rs 134,822.91 gross using 195 retry attempts, including 85 against guaranteed-fail cases blocked by compliance guardrails. Our pipeline recovers Rs 125,958.49 gross using only 170 attempts (a 25-attempt reduction) -- net of an assumed Rs 10.00/attempt cost, that's Rs 124,258.49 vs Rs 132,872.91, a Rs 8,614.42 (6.5%) net shortfall versus the naive baseline, with zero uncompliant executions (280 cases verified).

## g. Efficiency (Rs recovered per retry attempt)

This answers a different question than section d: not "how many total net dollars did each policy recover" but "how efficiently did each dollar of retry-attempt spend perform." A policy that retries fewer, better-targeted cases can win here even while trailing on total net dollars.

| Scenario | Retry Attempts | Gross Recovered | Net Recovered | Recovered Rs/Attempt |
|---|---|---|---|---|
| naive_no_guardrails (retry everyone, no compliance check) | 280 | Rs 142,186.62 | Rs 139,386.62 | Rs 507.81 |
| compliant_no_targeting (retry everyone guardrails allow, no ML/LLM targeting) | 195 | Rs 134,822.91 | Rs 132,872.91 | Rs 691.40 |
| pipeline (ML/LLM-targeted, guardrailed) | 170 | Rs 125,958.49 | Rs 124,258.49 | Rs 740.93 |

Efficiency lift per attempt fired (pipeline vs compliant_no_targeting): 7.2% -- each retry the pipeline fires recovers Rs 740.93 on average, vs Rs 691.40 for a guardrailed policy with no targeting. This is a per-attempt efficiency measure, distinct from the net_lift_pct in section d (which compares total net dollars across differently-sized retry sets).

## h. Net Lift -- Uncertainty & Sensitivity

- Net lift (%) point estimate: -6.5%
- 95% bootstrap CI (n=5000 resamples, case rows resampled with replacement): [-12.9%, -1.7%]
- Resamples with net_lift_pct >= 0%: 0.0%
- Breakeven cost per retry attempt (COST_PER_RETRY_ATTEMPT value at which pipeline_net_recovered == baseline_net_recovered): Rs 354.58 (current assumption: Rs 10.00)
