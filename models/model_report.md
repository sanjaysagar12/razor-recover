# Phase 3 Model Report

Run date: 2026-09-02 | Random seed: 42

Split sizes: train=196, val=42, test=42

## LogReg vs XGBoost

| Metric | Logistic Regression | XGBoost |
|---|---|---|
| Chosen hyperparameters | C=0.1 | {'max_depth': 3, 'learning_rate': 0.05, 'subsample': 0.8, 'colsample_bytree': 0.6, 'min_child_weight': 5, 'reg_lambda': 1.5}, n_estimators=67 |
| CV F1 (mean +/- std) | 0.5525 +/- 0.0787 | 0.4219 +/- 0.0694 |
| CV Brier (mean +/- std) | 0.2193 +/- 0.0169 | 0.2236 +/- 0.0120 |
| Test Precision | 0.4167 | 0.5714 |
| Test Recall | 0.2778 | 0.4444 |
| Test F1 | 0.3333 | 0.5000 |
| Test Brier score | 0.2108 | 0.2140 |

### Overfitting-instability check

CV std ratio (XGBoost / LogReg): F1=0.88x, Brier=0.71x

Not flagged: XGBoost's cross-validation standard deviation stays within 2x of logistic regression's.

## Naive baseline

A constant prediction equal to the training set's class prior gives a Brier score of
**0.2449** on the test set. Both trained models should beat this to be worth deploying.

## Calibration check (logreg_v1_2026-09-02, primary model)

Predictions bucketed into quantile bins by predicted probability:

| Probability bin | n | Mean predicted probability | Observed outcome rate |
|---|---|---|---|
| (0.0594, 0.212] | 11 | 0.1535 | 0.0000 |
| (0.212, 0.423] | 10 | 0.3608 | 0.6000 |
| (0.423, 0.503] | 10 | 0.4674 | 0.8000 |
| (0.503, 0.709] | 11 | 0.5797 | 0.3636 |

## Verdict

**PRIMARY_MODEL = logreg_v1_2026-09-02 (Logistic Regression).** On the held-out test set, logistic regression's Brier score (0.2108) is equal to or better than XGBoost's (0.2140). XGBoost does NOT clearly beat logistic regression on this dataset -- with only ~280 rows, the extra flexibility of a tree ensemble does not translate into better-calibrated probabilities than a simple, heavily regularized linear model. Rather than pick XGBoost by default, logistic regression is selected as PRIMARY_MODEL because it wins (or ties) on the proper scoring rule.
