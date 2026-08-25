# Synthetic Data — Label Generation Rules

Labels (`outcome`) are NOT random. Each row's recovery probability is computed
by `latent_recovery_prob()` in `generate_synthetic.py` from additive rules,
then sampled with `np.random.binomial(1, p)`, plus a ~4% random label flip so
the problem is not trivially separable.

## Rules (base probability starts at 0.5)

- `decline_code_bucket == CLEAR_HARD` -> -0.45 (stolen/lost/restricted card, invalid account: recovery is very unlikely)
- `decline_code_bucket == CLEAR_SOFT` -> +0.15 (insufficient funds / expired card: often resolves itself)
- `decline_code_bucket == AMBIGUOUS` -> +0.03 (do-not-honor style codes: intentionally kept near-uncertain)
- `historical_ptp_honor_rate` -> +0.42 * (rate - 0.5): customers who've honored promises-to-pay before are more likely to recover
- `retry_attempt_number` -> -0.03 per attempt beyond the first: retry fatigue
- `customer_tenure_days` -> +0.12 * (tenure/1800 - 0.5): longer-tenured customers recover more often
- `payment_rail == upi_autopay` AND `retry_attempt_number >= 4` -> -0.35 (NPCI 1-original + 3-retry cap exceeded; guardrail flag `npci_retry_cap_reached`)
- `payment_rail == upi_autopay` AND `is_peak_execution_window` -> -0.10 (10am-1pm NPCI peak window congestion; guardrail flag `peak_window_risk`)
- `amount_vs_historical_avg` far from 1.0 -> -0.20 * |ratio - 1|: anomalous amounts vs. the customer's history are riskier (guardrail flag `anomalous_amount` when |ratio-1| > 0.5)
- `ltv_tier` -> +0.05 (high), 0.0 (medium), -0.03 (low): minor loyalty-tier effect
- `issuer_bank_risk_tier` -> +0.03 (low_risk), 0.0 (medium_risk), -0.05 (high_risk): minor issuer-risk effect
- `decline_code_bucket == CLEAR_HARD` also sets guardrail flag `hard_decline`

Final probability is clipped to [0.03, 0.97]. Gaussian jitter is added to
`historical_ptp_honor_rate`, `amount_vs_historical_avg`, and
`customer_tenure_days` at generation time (independent measurement noise).
A ~4% random label flip is applied after sampling.

## Split
280 rows generated, stratified 80/20 train/holdout by
(decline_code_bucket, outcome), with holdout topped up to guarantee at least
15 AMBIGUOUS rows (the bucket most important for demonstrating genuine
model uncertainty).
