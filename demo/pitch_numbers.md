# Pitch Numbers -- Batch Simulation

Generated: 2026-08-27T14:47:19.473810+00:00

## a. Batch

- Cases in batch: 280
- Total amount at stake: Rs 347,524.00

## b. Gross Recovered

- Pipeline: Rs 125,958.49 (36.2% of total)
- Naive baseline (retry every case once, guardrailed): Rs 134,822.91 (38.8% of total)

Note: the pipeline's gross figure may be lower than or comparable to the naive baseline's -- it retries fewer, more carefully selected cases, so it is expected to leave some recoverable revenue on the table in exchange for far fewer wasted attempts. Gross recovered-$ is reported here for transparency but is not the headline number (see Net Recovered below).

## c. Retry Attempts

- Pipeline retry attempts: 171
- Naive baseline retry attempts: 195
- Wasted retries avoided (baseline - pipeline): 24
- Of the baseline's attempts, blocked into guaranteed-fail cases by compliance guardrails (hard declines, NPCI/network retry caps) had it not been guardrailed: 85

## d. Net Recovered (headline)

Assumes Rs 10.00 cost per retry attempt executed (see COST_PER_RETRY_ATTEMPT in pipeline/run_batch.py -- a placeholder assumption, not derived from data; tune as real per-attempt cost figures become available).

- Pipeline net recovered: Rs 124,248.49
- Naive baseline net recovered: Rs 132,872.91
- Net lift (absolute): Rs -8,624.42 (shortfall)
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

Naive retry-everything recovers Rs 134,822.91 gross using 195 retry attempts, including 85 against guaranteed-fail cases blocked by compliance guardrails. Our pipeline recovers Rs 125,958.49 gross using only 171 attempts (a 24-attempt reduction) -- net of an assumed Rs 10.00/attempt cost, that's Rs 124,248.49 vs Rs 132,872.91, a Rs 8,624.42 (6.5%) net shortfall versus the naive baseline, with zero uncompliant executions (280 cases verified).
