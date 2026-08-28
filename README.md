# Revenue Recovery Agent (Razorpay AI Buildathon — Track 03)

AI revenue-recovery agent for failed subscription payments, built against
Razorpay's **test-mode** APIs.

> **Status:** Phases 1-8 implemented — data generation, tree model + SHAP,
> confidence gate, LLM layer, guardrails, batch simulation/audit log, and
> the demo CLI below.

## Project structure

```
revenue-recovery/
├── data/                     # datasets (placeholder for now)
├── models/models/artifacts/  # trained model artifacts (placeholder for now)
├── pipeline/                 # scripts: Razorpay client, verification, etc.
├── logs/                     # run logs
├── demo/                     # demo assets/scripts
├── requirements.txt
├── .env.example
└── README.md
```

## Setup

### 1. Create and activate the virtual environment

```bash
python -m venv venv
# Windows (PowerShell)
./venv/Scripts/Activate.ps1
# macOS/Linux
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Get Razorpay TEST MODE API keys

1. Log in to the [Razorpay Dashboard](https://dashboard.razorpay.com/).
2. Switch to **Test Mode** using the toggle in the top bar.
3. Go to **Account & Settings → API Keys**.
4. Click **Generate Test Key**.
5. Copy the generated **Key Id** (starts with `rzp_test_`) and **Key Secret**
   — the secret is shown only once, so save it immediately.

Never use live-mode keys (`rzp_live_...`) with this project.

### 4. Configure your `.env`

```bash
cp .env.example .env
```

Then edit `.env` and fill in:

- `RAZORPAY_KEY_ID` — your test-mode Key Id (`rzp_test_...`)
- `RAZORPAY_KEY_SECRET` — your test-mode Key Secret
- `ANTHROPIC_API_KEY` — your Anthropic API key (used in later phases)

`.env` is gitignored — never commit real credentials.

### 5. Verify the setup (Phase 1 exit check)

Run the verification script. It creates a test subscription plan via the
Razorpay API, then fetches it back, to confirm your credentials work
end to end:

```bash
python pipeline/verify_setup.py
```

Expected output on success:

```
Revenue Recovery — Phase 1 setup verification
--------------------------------------------------
[1/2] Creating test plan (POST /v1/plans) ...
      OK — created plan plan_xxxxxxxxxxxxx
[2/2] Fetching plan back (GET /v1/plans/:id) ...
      OK — fetched plan plan_xxxxxxxxxxxxx (50000 INR / monthly)
--------------------------------------------------
SUCCESS: Razorpay test-mode credentials are working end to end.
```

If it fails, double-check that `.env` exists and contains valid test-mode
keys (`RAZORPAY_KEY_ID` must start with `rzp_test_`).

## Pipeline overview

1. **Data** (`data/`) — 280-row synthetic decline-case dataset
   (`generate_synthetic.py`, seed 42), split into `train.csv`/`holdout.csv`.
2. **Tree models** (`models/train_tree_models.py`) — logistic regression +
   regularized XGBoost, compared on precision/recall/F1/Brier score; the
   winner is written to `models/artifacts/` as `PRIMARY_MODEL`.
3. **Confidence gate + SHAP** (`pipeline/confidence_gate.py`,
   `pipeline/shap_extract.py`) — scores every case, extracts its SHAP top-5
   features, and routes ambiguous cases to the LLM while clear cases get a
   template action.
4. **LLM layer** (`pipeline/llm_layer.py`) — one structured, schema-validated
   call per routed case, reasoning over the SHAP top-5. The layer supports
   either provider (`ClaudeAdapter`/`GeminiAdapter`, selected by
   `LLM_PROVIDER` in `.env`, no code change needed) and both return the
   identical validated shape — but **the config actually checked into this
   repo's `.env` is `LLM_PROVIDER=gemini` / `GEMINI_MODEL=gemini-3.1-flash-lite`**,
   and that is the provider that produced every LLM output currently in
   `logs/audit_log.csv` (`model_version: gemini:gemini-3.1-flash-lite` on
   every routed row) — no Claude call has actually been made against this
   batch. Nothing in the code or comments states *why* Gemini was picked
   over Claude as the default (no cost/quota/rate-limit note tied to that
   choice is recorded anywhere) — flagging that rather than guessing at a
   reason. Never acts directly, and never crashes the batch on failure —
   see **Demo → resilience check** below.
5. **Guardrails** (`pipeline/guardrails.py`) — deterministic compliance
   rules (hard-decline exclusion, NPCI/network retry caps, peak-window
   rejection, confidence-floor escalation) that run on every case and can
   override the proposed action.
6. **Audit log + batch simulation** (`pipeline/run_batch.py`) — orchestrates
   1-5 for the full batch, writes `logs/audit_log.csv` (one row per case)
   and `demo/pitch_numbers.md` (recovered-amount simulation vs. a
   guardrailed naive "retry everything" baseline).

## Demo (Phase 8)

The demo CLI consumes the outputs above — it does not re-run the pipeline
or make any live LLM API call. Showcase cases and their LLM outputs are
read from the existing `logs/audit_log.csv`.

```bash
# 1. (one-time; already done in this repo's checked-in audit log) backfill
#    the routing_rationale column into logs/audit_log.csv -- deterministic,
#    no LLM/network call, cross-checked against the existing log before
#    writing (see the file's docstring for why this is safe without a full
#    pipeline/run_batch.py re-run)
python pipeline/backfill_routing_rationale.py

# 2. (one-time, or after a fresh pipeline run) pick the 4 showcase cases
python demo/select_showcase_cases.py

# 3. run the demo
python demo/cli_demo.py
```

**What to expect:** a plain fixed-width text report (no color, no live
network calls) printed in this order:

1. A 2-3 line architecture summary.
2. The LogReg vs. XGBoost precision/recall/F1/Brier table from
   `models/model_report.md`.
3. Four showcase cases, one per pipeline path — case facts, tree score, the
   confidence-gate's own routing rationale (score vs. band, ambiguous-code
   flag, which trigger fired or why neither did), SHAP top-3, LLM reasoning
   (if invoked), guardrail verdict (passed, or overridden and by which
   rule), and final action.
4. The headline result: three scenarios (naive/no-guardrails, compliant/
   no-targeting, full pipeline) side by side — retry attempts, net
   recovered Rs, % of total, Rs recovered per retry attempt, and compliance
   violations — computed live from `logs/audit_log.csv` (not read from a
   pre-written file), with an independent manual sum cross-checked to match
   exactly (see Verification below). See **Headline number** below for why
   it's three scenarios and not one pipeline-vs-baseline number.

Optional resilience rehearsal — proves the pipeline degrades to
`requires_human_review: true` instead of crashing when the LLM adapter
fails on every attempt (no network call is made in either mode):

```bash
DEMO_SIMULATE_LLM_FAILURE=1 python demo/cli_demo.py   # bash
$env:DEMO_SIMULATE_LLM_FAILURE=1; python demo/cli_demo.py  # PowerShell
```

### Model comparison (from `models/model_report.md`)

| Metric | Logistic Regression | XGBoost |
|---|---:|---:|
| Test Precision | 0.4167 | 0.5714 |
| Test Recall | 0.2778 | 0.4444 |
| Test F1 | 0.3333 | 0.5000 |
| Test Brier score | 0.2108 | 0.2140 |

`PRIMARY_MODEL = logreg_v1_2026-08-25` — logistic regression wins on Brier
score (a proper scoring rule for calibration), so it was selected over
XGBoost despite XGBoost's higher F1, per `models/model_report.md`'s
verdict.

### Headline number

Computed from the full 280-case batch in `logs/audit_log.csv`, assuming
Rs 10.00 cost per retry attempt executed (`COST_PER_RETRY_ATTEMPT` in
`pipeline/run_batch.py`, a placeholder, not derived from data). Reported as
**three** scenarios, not two — comparing the pipeline's recovered-$ against
a single "naive baseline" figure hid whether that baseline was itself
obeying compliance rules, which matters: an unguardrailed policy recovers
more raw revenue partly *because* it executes retries compliance rules
forbid (see `pipeline/run_batch.compute_three_scenario_simulation`):

| Scenario | Attempts | Net recovered | % of total | Rs/attempt | Compliance violations |
|---|---:|---:|---:|---:|---:|
| Naive (no guardrails at all) | 280 | Rs 139,386.62 | 40.1% | Rs 507.81 | **85** |
| Compliant (guardrails, no ML/LLM targeting) | 195 | Rs 132,872.91 | 38.2% | Rs 691.40 | 0 |
| **Pipeline (guardrails + ML/LLM)** | **171** | **Rs 124,248.49** | **35.8%** | **Rs 736.60** | **0** |

- **Naive** proposes a retry for literally every case with zero rule
  checks. 85 of its 280 retries (30%) would fire a hard-decline / NPCI-cap
  / network-cap guardrail — money it can only "recover" by breaking a
  compliance rule the other two scenarios respect. Its higher raw net-Rs
  figure is **not** a fair number to headline against the pipeline on its
  own.
- **Compliant** is a blanket "retry everything the guardrails allow, no
  targeting" policy — it isolates what respecting the rules costs/gains
  by itself, separate from the ML/LLM layer's judgment.
- **Pipeline** is the actual ML/LLM-targeted, fully guardrailed system.

**The strongest number in this simulation is recovered-Rs per retry
attempt** — pipeline efficiency (Rs 736.60/attempt) beats the compliant
baseline by **+6.5%** and the naive baseline by **+45.1%**. The pipeline
retries fewer cases, but the ones it does retry recover meaningfully more
per attempt than either blanket policy — that's the value the ML/LLM
targeting layer adds, isolated from the guardrails' effect.

On raw net-Rs, the pipeline runs behind both baselines on this batch: -6.5%
vs. the compliant baseline (Rs -8,624.42) and -10.9% vs. the naive baseline
(Rs -15,138.13, not apples-to-apples given its 85 compliance violations).
What the pipeline unambiguously wins on: **0 non-compliant executions** out
of 280 cases, vs. 85 such cases the naive policy would execute into. See
`PHASE7_REPORT.md` for the full before/after analysis of the two-scenario
version of this number (including the real Gemini run that narrowed the
pipeline-vs-compliant-baseline gap from an initial -23.4% to -6.5%) and the
open levers (`COST_PER_RETRY_ATTEMPT` realism, a stronger LLM tier) that
would likely close it further.

### On vendor-published recovery-rate figures

Numbers like Stripe's oft-cited "~55% of failed payments recovered" or
Adyen's incremental-recovery-uplift figures are **marketing claims from
those companies' own case studies**, measured on their own live production
traffic, under their own methodology, which is not published in enough
detail to reproduce. Every number above — the -6.5%/-10.9% net-lift
figures, the Rs 736.60/attempt efficiency figure, all three scenarios in
the table — **is our own measured simulation, on our own 280-row
*synthetic* dataset**, against baselines we defined and computed ourselves.
None of it is a benchmark against Stripe, Adyen, or any other vendor, and
none of it should be read as a claim of parity (or gap) with any vendor's
published number — the datasets, traffic, and methodology are not
comparable. Treat these numbers as evidence the pipeline's guardrail/ML/LLM
logic behaves correctly and measurably on a controlled dataset, not as a
production recovery-rate claim.

### Verification

Re-run anytime with:

```bash
python pipeline/validate_audit_log.py   # 7 automated checks against the audit log
python demo/cli_demo.py                 # 3x in a row: identical output except the
                                         # trailing "Elapsed: X.XXs" line, which
                                         # measures the run itself and is expected
                                         # to jitter by a few hundredths of a second
```

Both the audit log and the demo CLI are deterministic: data generation
(`data/generate_synthetic.py`), model training
(`models/train_tree_models.py`), and the SHAP sanity check all use a fixed
`random_seed=42`, and `demo/cli_demo.py` only reads static files (plus, in
the LLM-invoked showcase case, the LLM output already recorded in
`logs/audit_log.csv` from a prior real API run) — it never samples or
calls a live model at demo time.

## Roadmap

Remaining, not part of Phase 8: webhook handling for `payment.failed` /
`subscription.*` events (this repo runs against a pre-collected batch, not
live webhooks).
