"""
Synthetic data generator for the payment-decline recovery ML pipeline
(Razorpay AI Buildathon, Track 03 — AI Revenue Recovery).

Generates a case-level dataset where `outcome` (1 = payment recovered on
retry) is driven by an explicit, documented latent-probability function of
the features — not random noise — so a downstream XGBoost/LogisticRegression
model has real signal to learn. Run: python data/generate_synthetic.py
"""
import sys
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

SEED = 42
N_ROWS = 280
OUT_DIR = "data"

rng = np.random.default_rng(SEED)

DECLINE_CODES = {
    "CLEAR_SOFT": (["insufficient_funds", "51_insufficient_funds", "expired_card_soft"], [0.5, 0.3, 0.2]),
    "CLEAR_HARD": (["stolen_card", "lost_card", "restricted_card", "invalid_account"], [0.3, 0.3, 0.2, 0.2]),
    "AMBIGUOUS": (["05_do_not_honor", "generic_decline", "issuer_unavailable"], [0.7, 0.18, 0.12]),
}
BUCKETS, BUCKET_P = ["CLEAR_SOFT", "CLEAR_HARD", "AMBIGUOUS"], [0.5, 0.25, 0.25]


def latent_recovery_prob(row: dict) -> float:
    """Additive-rule probability of recovery, 0.03-0.97. See generation_notes.md."""
    p = 0.5
    bucket = row["decline_code_bucket"]
    if bucket == "CLEAR_HARD":
        p -= 0.45
    elif bucket == "CLEAR_SOFT":
        p += 0.15
    else:  # AMBIGUOUS: small nudge so it lands near 0.5 once other terms average out
        p += 0.03

    p += 0.42 * (row["historical_ptp_honor_rate"] - 0.5)          # trust track record
    p -= 0.03 * (row["retry_attempt_number"] - 1)                  # retry fatigue
    p += 0.12 * (row["customer_tenure_days"] / 1800 - 0.5)         # loyalty
    if row["payment_rail"] == "upi_autopay":
        if row["retry_attempt_number"] >= 4:
            p -= 0.35                                               # NPCI retry-cap exceeded
        if row["is_peak_execution_window"]:
            p -= 0.10                                               # peak-window congestion
    p -= 0.20 * abs(row["amount_vs_historical_avg"] - 1.0)          # anomalous amount
    p += {"low": -0.03, "medium": 0.0, "high": 0.05}[row["ltv_tier"]]
    p += {"low_risk": 0.03, "medium_risk": 0.0, "high_risk": -0.05}[row["issuer_bank_risk_tier"]]
    return float(np.clip(p, 0.03, 0.97))


def gen_row(i: int) -> dict:
    bucket = rng.choice(BUCKETS, p=BUCKET_P)
    codes, code_p = DECLINE_CODES[bucket]
    retry_n = int(rng.choice([1, 2, 3, 4, 5], p=[0.40, 0.28, 0.16, 0.10, 0.06]))
    payment_rail = rng.choice(["card", "upi_autopay", "emandate"], p=[0.5, 0.35, 0.15])
    row = {
        "case_id": f"case_{i:04d}",
        "decline_code_bucket": bucket,
        "decline_code": rng.choice(codes, p=code_p),
        "retry_attempt_number": retry_n,
        "hours_since_last_attempt": round(float(rng.uniform(0, 72)), 2),
        "customer_tenure_days": int(np.clip(rng.uniform(0, 1800) + rng.normal(0, 15), 0, 1800)),
        "ltv_tier": rng.choice(["low", "medium", "high"], p=[0.4, 0.4, 0.2]),
        "historical_ptp_honor_rate": float(np.clip(rng.beta(2, 2) + rng.normal(0, 0.02), 0, 1)),
        "time_of_day_bucket": rng.choice(["early_morning", "morning", "afternoon", "evening", "night"]),
        "day_of_week": rng.choice(["mon", "tue", "wed", "thu", "fri", "sat", "sun"]),
        "issuer_bank_risk_tier": rng.choice(["low_risk", "medium_risk", "high_risk"], p=[0.5, 0.35, 0.15]),
        "payment_rail": payment_rail,
        "amount": round(float(np.clip(rng.lognormal(7.0, 0.6), 199, 9999)), 2),
        "amount_vs_historical_avg": float(np.clip(rng.normal(1.0, 0.22) + rng.normal(0, 0.05), 0.1, 3.0)),
        "is_peak_execution_window": bool(rng.random() < 0.15),
        "prior_retry_success_count": int(rng.integers(0, max(retry_n, 1))),
    }
    flags = []
    if payment_rail == "upi_autopay" and retry_n >= 4:
        flags.append("npci_retry_cap_reached")
    if payment_rail == "upi_autopay" and row["is_peak_execution_window"]:
        flags.append("peak_window_risk")
    if bucket == "CLEAR_HARD":
        flags.append("hard_decline")
    if abs(row["amount_vs_historical_avg"] - 1.0) > 0.5:
        flags.append("anomalous_amount")
    row["guardrail_flags"] = ";".join(flags)
    return row


rows = [gen_row(i) for i in range(N_ROWS)]
probs = np.array([latent_recovery_prob(r) for r in rows])
outcomes = rng.binomial(1, probs)

# Low-rate label flip (~4%) so the problem isn't trivially separable
flip_mask = rng.random(N_ROWS) < 0.04
outcomes = np.where(flip_mask, 1 - outcomes, outcomes)

df = pd.DataFrame(rows)
df["outcome"] = outcomes

# --- Stratified 80/20 split by (decline_code_bucket, outcome) ---------------
train_idx, holdout_idx = [], []
for bucket in BUCKETS:
    for out_val in [0, 1]:
        idx = df.index[(df.decline_code_bucket == bucket) & (df.outcome == out_val)].to_numpy().copy()
        rng.shuffle(idx)
        n_hold = max(1, round(len(idx) * 0.2))
        holdout_idx.extend(idx[:n_hold])
        train_idx.extend(idx[n_hold:])
train_idx, holdout_idx = set(train_idx), set(holdout_idx)

# Guarantee holdout has >=15 AMBIGUOUS rows by topping up from train
amb_holdout = [i for i in holdout_idx if df.loc[i, "decline_code_bucket"] == "AMBIGUOUS"]
if len(amb_holdout) < 15:
    amb_train = [i for i in train_idx if df.loc[i, "decline_code_bucket"] == "AMBIGUOUS"]
    rng.shuffle(np.array(amb_train, dtype=object)) if amb_train else None
    need = 15 - len(amb_holdout)
    for i in list(amb_train)[:need]:
        train_idx.discard(i)
        holdout_idx.add(i)

train_df = df.loc[sorted(train_idx)].reset_index(drop=True)
holdout_df = df.loc[sorted(holdout_idx)].reset_index(drop=True)
train_df.to_csv(f"{OUT_DIR}/train.csv", index=False)
holdout_df.to_csv(f"{OUT_DIR}/holdout.csv", index=False)

# ============================== VALIDATION ==================================
results = []  # (check name, pass bool, detail str)


def onehot_auc(feature_cols, train, hold):
    X_tr = pd.get_dummies(train[feature_cols])
    X_ho = pd.get_dummies(hold[feature_cols]).reindex(columns=X_tr.columns, fill_value=0)
    clf = LogisticRegression(max_iter=1000).fit(X_tr, train["outcome"])
    return roc_auc_score(hold["outcome"], clf.predict_proba(X_ho)[:, 1])


# 1. Class balance
pos_rate = df["outcome"].mean()
results.append(("Class balance (0.35-0.65)", 0.35 <= pos_rate <= 0.65, f"{pos_rate:.3f}"))

# 2. Signal check
signal_auc = onehot_auc(["decline_code", "historical_ptp_honor_rate"], train_df, holdout_df)
results.append(("Signal check AUC (>0.65)", signal_auc > 0.65, f"{signal_auc:.3f}"))

# 3. Ambiguous-bucket sanity
amb = df[df.decline_code_bucket == "AMBIGUOUS"]
amb_rate = amb["outcome"].mean()
results.append(("Ambiguous outcome rate (0.40-0.60)", 0.40 <= amb_rate <= 0.60, f"{amb_rate:.3f}"))

# 4. No-leakage check (single feature at a time)
feature_cols = [c for c in df.columns if c not in ("case_id", "outcome", "guardrail_flags")]
leak_flags = []
for col in feature_cols:
    try:
        auc = onehot_auc([col], train_df, holdout_df)
    except ValueError:
        continue
    if auc > 0.95:
        leak_flags.append(f"{col} (AUC={auc:.3f})")
results.append(("No single-feature leakage (AUC<=0.95)", len(leak_flags) == 0, "; ".join(leak_flags) or "none"))

# 5. Distribution sanity
dist_ok = True
dist_lines = []
for col, lo, hi in [("amount", 199, 9999), ("amount_vs_historical_avg", 0, 5), ("historical_ptp_honor_rate", 0, 1)]:
    s = df[col]
    ok = s.std() > 1e-6 and s.min() >= lo - 1e-6 and s.max() <= hi + 1e-6
    dist_ok &= ok
    dist_lines.append(f"{col}: mean={s.mean():.3f} std={s.std():.3f} min={s.min():.3f} max={s.max():.3f}")
results.append(("Distribution sanity (nonzero variance, plausible range)", dist_ok, " | ".join(dist_lines)))

# 6. Split verification
split_ok = True
crosstabs = {}
for name, split in [("train", train_df), ("holdout", holdout_df)]:
    ct = pd.crosstab(split.decline_code_bucket, split.outcome)
    crosstabs[name] = ct
    if (ct.sum(axis=1) < 10).any():
        split_ok = False
results.append(("Split verification (>=10 rows/bucket per split)", split_ok, "see cross-tabs below"))

# ============================== SUMMARY ======================================
print("=" * 78)
print("SYNTHETIC DATA GENERATION — VALIDATION SUMMARY")
print("=" * 78)
all_pass = True
for name, ok, detail in results:
    status = "PASS" if ok else "FAIL"
    all_pass &= ok
    print(f"[{status}] {name}: {detail}")
print("-" * 78)
for name, ct in crosstabs.items():
    print(f"{name} cross-tab (decline_code_bucket x outcome):")
    print(ct)
print("-" * 78)
print(f"Total rows: {len(df)} | train: {len(train_df)} | holdout: {len(holdout_df)}")
print(f"Class balance: {pos_rate:.1%} positive")
print(f"Signal-check AUC: {signal_auc:.3f}")
print(f"Ambiguous-bucket outcome rate: {amb_rate:.3f}")
print(f"Leakage flags: {leak_flags or 'none'}")
print("=" * 78)
print("OVERALL:", "PASS" if all_pass else "FAIL")

# --- generation_notes.md -----------------------------------------------------
notes = """# Synthetic Data — Label Generation Rules

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
"""
with open(f"{OUT_DIR}/generation_notes.md", "w") as f:
    f.write(notes)

if not all_pass:
    print("\nOne or more validation checks FAILED. See summary above.")
    sys.exit(1)
sys.exit(0)
