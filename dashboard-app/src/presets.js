// Presets engineered against pipeline/guardrails.py's actual GUARDRAIL_RULES.
// See README_DASHBOARD.md for which are deterministic vs LLM-dependent.
// `tone` is presentation-only -- 'override' for rules that flip
// guardrail_overrode=true, 'review' for the escalation-only rules that
// leave guardrail_overrode=false by design.
export const PRESETS = [
  {
    name: 'Hard decline override',
    rule: 'hard_decline_excluded',
    description: "This decline code (stolen card) can never be fixed by retrying. The hard_decline_excluded guardrail steps in and overrides any retry the model proposes, routing the case straight to human escalation instead.",
    tone: 'override',
    payload: { event_type: 'payment.failed', case: {
      decline_code: 'stolen_card', decline_code_bucket: 'CLEAR_HARD', decline_code_is_ambiguous: true,
      amount: 1200, retry_attempt_number: 1, payment_rail: 'card', ltv_tier: 'high',
      historical_ptp_honor_rate: 0.9, customer_tenure_days: 1000, prior_retry_success_count: 3,
      issuer_bank_risk_tier: 'low_risk', amount_vs_historical_avg: 1.0, hours_since_last_attempt: 2,
      email: 'hard.decline.demo@example.com', contact: '9123456781',
      customer_id: 'cust_hard_decline_demo', customer_key: 'cust_hard_decline_demo',
    }},
  },
  {
    name: 'NPCI retry cap exceeded',
    rule: 'npci_retry_cap_reached',
    description: "UPI Autopay retries are capped by NPCI rules. Once a case has already hit 4 retry attempts, the npci_retry_cap_reached guardrail blocks any further retry and overrides the proposed action to stay compliant.",
    tone: 'override',
    payload: { event_type: 'payment.failed', case: {
      decline_code: 'insufficient_funds', decline_code_bucket: 'CLEAR_SOFT', decline_code_is_ambiguous: false,
      amount: 800, retry_attempt_number: 4, payment_rail: 'upi_autopay', ltv_tier: 'high',
      historical_ptp_honor_rate: 0.9, customer_tenure_days: 900, prior_retry_success_count: 3,
      issuer_bank_risk_tier: 'low_risk', amount_vs_historical_avg: 1.0, hours_since_last_attempt: 2,
      email: 'npci.cap.demo@example.com', contact: '9123456782',
      customer_id: 'cust_npci_cap_demo', customer_key: 'cust_npci_cap_demo',
    }},
  },
  {
    // npci_peak_window only fires on an LLM-scheduled retry_scheduled whose
    // action_scheduled_for lands in [10:00,13:00) IST -- the LLM is given
    // no "current date/time" context (see pipeline/prompts.py), so its
    // chosen timestamp is inherently ungrounded and this preset is NOT
    // deterministic. Click multiple times if the first attempt misses.
    name: 'Peak execution window block',
    rule: 'npci_peak_window',
    description: "NPCI blocks UPI Autopay retries scheduled between 10:00 and 13:00 IST. If the LLM happens to schedule a retry inside that window, the npci_peak_window guardrail overrides it. The LLM isn't given the current time, so this only fires when its chosen slot lands there -- click again if it doesn't trigger.",
    tone: 'override',
    payload: { event_type: 'payment.failed', case: {
      decline_code: 'issuer_unavailable', decline_code_bucket: 'AMBIGUOUS', decline_code_is_ambiguous: true,
      amount: 650, retry_attempt_number: 2, payment_rail: 'upi_autopay', ltv_tier: 'medium',
      historical_ptp_honor_rate: 0.55, customer_tenure_days: 400, prior_retry_success_count: 1,
      issuer_bank_risk_tier: 'medium_risk', amount_vs_historical_avg: 1.0, hours_since_last_attempt: 12,
      is_peak_execution_window: true,
      email: 'peak.window.demo@example.com', contact: '9123456783',
      customer_id: 'cust_peak_window_demo', customer_key: 'cust_peak_window_demo',
    }},
  },
  {
    // retry_attempt_number must stay below 5 (network_retry_cap_exceeded's
    // threshold) -- cumulative_retries_this_txn defaults to it, and at 5 it
    // ALSO trips that rule, confounding this preset with a second override.
    // Verified: at 2, this cleanly fires requires_human_review_floor alone.
    name: 'Confidence floor breach',
    rule: 'requires_human_review_floor',
    description: "The tree model's confidence score falls below the safe threshold for this decline. Rather than let the pipeline decide on its own, requires_human_review_floor escalates the case to a human -- it flags for review without overriding the proposed action.",
    tone: 'review',
    payload: { event_type: 'payment.failed', case: {
      decline_code: 'expired_card_soft', decline_code_bucket: 'CLEAR_SOFT', decline_code_is_ambiguous: false,
      amount: 500, retry_attempt_number: 2, payment_rail: 'card', ltv_tier: 'low',
      historical_ptp_honor_rate: 0.05, customer_tenure_days: 10, prior_retry_success_count: 0,
      issuer_bank_risk_tier: 'high_risk', amount_vs_historical_avg: 2.5, hours_since_last_attempt: 1,
      email: 'confidence.floor.demo@example.com', contact: '9123456784',
      customer_id: 'cust_confidence_floor_demo', customer_key: 'cust_confidence_floor_demo',
    }},
  },
  {
    // Also escalation-only, like Confidence floor breach -- guardrail_overrode
    // stays False by design. Uses run_case.MalformedTestAdapter (see
    // pipeline/run_case.py) instead of a real LLM call, since a real
    // provider's structured-output mode can't be reliably forced to fail.
    name: 'Malformed LLM output',
    rule: 'requires_human_review_floor',
    description: "Simulates the LLM returning a response that fails schema validation. When that happens, the pipeline can't trust the AI's output, so requires_human_review_floor escalates the case to a human instead of guessing at what the model meant.",
    tone: 'review',
    payload: { event_type: 'payment.failed', case: {
      decline_code: '05_do_not_honor', decline_code_bucket: 'AMBIGUOUS', decline_code_is_ambiguous: true,
      amount: 700, retry_attempt_number: 2, payment_rail: 'card', ltv_tier: 'medium',
      historical_ptp_honor_rate: 0.5, customer_tenure_days: 300, prior_retry_success_count: 1,
      issuer_bank_risk_tier: 'medium_risk', amount_vs_historical_avg: 1.0, hours_since_last_attempt: 5,
      _force_malformed_llm: true,
      email: 'malformed.llm.demo@example.com', contact: '9123456785',
      customer_id: 'cust_malformed_llm_demo', customer_key: 'cust_malformed_llm_demo',
    }},
  },
];

// Real-Razorpay-shape example -- same envelope structure
// test_webhook_locally.py's SAMPLE_PAYMENT_FAILED uses (Phase 21). Posted to
// /api/trigger-webhook-shaped, which runs it through the actual
// map_payload_to_case() mapper instead of the internal-shape shortcut above.
export function buildRazorpayShapeExample() {
  return {
    entity: 'event',
    account_id: 'acc_test000000000001',
    event: 'payment.failed',
    contains: ['payment'],
    payload: {
      payment: {
        entity: {
          id: 'pay_dashboard_example_' + Date.now(),
          entity: 'payment',
          amount: 66242,
          currency: 'INR',
          status: 'failed',
          method: 'upi',
          vpa: 'test@upi',
          email: 'dashboard.example@example.com',
          contact: '+919999999999',
          customer_id: 'cust_dashboard_example',
          error_code: 'BAD_REQUEST_ERROR',
          error_description: 'Payment failed due to issuer unavailability',
          error_source: 'issuer',
          error_step: 'payment_authorization',
          error_reason: 'issuer_unavailable',
          created_at: Math.floor(Date.now() / 1000),
        },
      },
    },
    created_at: Math.floor(Date.now() / 1000),
  };
}
