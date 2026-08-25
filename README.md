# Revenue Recovery Agent (Razorpay AI Buildathon — Track 03)

AI revenue-recovery agent for failed subscription payments, built against
Razorpay's **test-mode** APIs.

> **Phase 1 status:** environment setup + Razorpay integration only. No
> webhook handling and no ML/LLM pipeline yet — those are later phases.

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

## Roadmap

This is Phase 1 of an 8-phase build. Not yet implemented:

- Webhook handling for `payment.failed` / `subscription.*` events
- Feature engineering + churn/recovery-likelihood model (xgboost)
- SHAP-based explainability
- LLM-driven recovery messaging (Anthropic)
- Demo flow
