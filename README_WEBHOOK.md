# Webhook Receiver

Flask app (`webhook_receiver.py`) that receives Razorpay webhooks, verifies
their signature, maps the event into the pipeline's case schema, and runs it
through `confidence_gate -> shap_extract -> (conditionally) llm_layer ->
guardrails`, logging one row per case to `logs/audit_log.csv`.

## Architecture note

`pipeline/shap_extract.py` was originally built to score cases already baked
into `data/train.csv` / `data/holdout.csv` (batch simulation, keyed by
`case_id` lookup). A live webhook case is never one of those 280 rows, so a
new function, `shap_extract.score_new_case()`, was added (pure addition, no
existing function touched) that scores an arbitrary case dict against the
same cached model + SHAP explainer. Everything downstream of that
(`confidence_gate.route_case`, `run_batch.build_audit_row`, which itself
calls `llm_layer` and `guardrails`) is reused unmodified.

The confidence-gate probability band is normally refit per batch from that
batch's own score distribution; for live single-case requests there's no
batch to refit against, so the band is fit once from the historical
train+holdout batch at process startup and reused for every webhook case.

## Running

```bash
python webhook_receiver.py
```

Listens on `0.0.0.0:5555`. Requires `.env` (copy from `.env.example`) with at
least `RAZORPAY_WEBHOOK_SECRET` set — the process refuses to start without
it. On startup it warms up the pipeline (loads the model, fits the SHAP
background, computes the confidence-gate band), which takes a few seconds.

With the Cloudflare Tunnel already pointed at `localhost:5555`, the public
endpoint is `https://razor-recover.heracle.fit/webhook/razorpay`.

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
- [ ] After saving, use the dashboard's "Test Webhook" button (or trigger a
      real test-mode payment failure) and check `logs/webhook_receiver.log` +
      `logs/audit_log.csv` for the resulting row

## Known limitations / deviations from the task spec

- **Decline-code mapping is best-effort.** The model was trained on a small
  synthetic decline-code taxonomy (`pipeline/../data/generate_synthetic.py`),
  not Razorpay's real `error_reason` vocabulary. `webhook_receiver.py` maps
  common reasons to the nearest synthetic bucket; anything unrecognized falls
  back to `generic_decline` (the AMBIGUOUS bucket, which always routes to the
  LLM rather than being auto-actioned on a guessed mapping) and logs a
  warning.
- **No customer history yet.** `data/customer_lookup.json` is created empty.
  Until populated (keyed by Razorpay `customer_id`, with
  `customer_tenure_days` / `ltv_tier` / `historical_ptp_honor_rate`), every
  case uses synthetic mid-range defaults for those three fields, logged each
  time.
- `hours_since_last_attempt`, `issuer_bank_risk_tier`,
  `amount_vs_historical_avg`, and `prior_retry_success_count` aren't in any
  Razorpay webhook payload and have no lookup source — they always use fixed
  synthetic defaults (see constants at the top of `webhook_receiver.py`).
