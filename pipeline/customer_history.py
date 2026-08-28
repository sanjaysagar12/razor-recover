"""
Live-webhook customer history store (SQLite).

Nothing here touches the synthetic training pipeline (data/generate_synthetic.py,
models/train_tree_models.py, pipeline/shap_extract.py's batch loading) -- this
is exclusively for webhook_receiver.py to stop hardcoding
customer_tenure_days=365 / ltv_tier=medium / historical_ptp_honor_rate=0.5 as
fallback defaults for every real case regardless of actual customer.

Keyed by customer_key: Razorpay customer_id when the webhook payload has one,
else "sub:<subscription_id>" as a fallback (subscriptions carry a stable id
even for payments where the payment entity itself has no customer_id), else
None if neither is present -- callers must handle the None case themselves
(no history to look up).

Storage: data/customer_history.db, one table. SQLite chosen over a JSON file
because updates here are read-modify-write per event (tenure lookup + a
retry-outcome counter update) and SQLite gives that for free without a
hand-rolled file lock.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "customer_history.db"

# Neutral defaults for a first-seen customer -- deliberately the midpoint of
# the training distributions (see data/generate_synthetic.py), not a guess
# at any real customer's behavior. Tenure starts counting from today.
DEFAULT_LTV_TIER = "medium"
DEFAULT_HISTORICAL_PTP_HONOR_RATE = 0.5


@contextmanager
def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_history (
                customer_key TEXT PRIMARY KEY,
                tenure_start_date TEXT NOT NULL,
                ltv_tier TEXT NOT NULL,
                historical_ptp_honor_rate REAL NOT NULL,
                prior_retry_success_count INTEGER NOT NULL DEFAULT 0,
                total_retry_outcomes INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_or_create_customer(customer_key: str | None) -> tuple[dict, bool]:
    """Returns (fields, is_first_seen).

    fields always has all four keys (customer_tenure_days, ltv_tier,
    historical_ptp_honor_rate, prior_retry_success_count) regardless of
    branch, so callers never need an extra None-check.

    is_first_seen=True means this call just INSERTed a brand-new record with
    neutral defaults -- distinguish this from is_first_seen=False (a real
    prior-history record) in both the audit row and application logs, since
    "we have no idea" and "we checked and this is what we found" are
    different facts.

    customer_key=None (no customer_id or subscription_id in the webhook
    payload) skips the DB entirely and returns first-seen-shaped defaults
    without persisting anything -- there's no stable key to store against.
    """
    if not customer_key:
        return (
            {
                "customer_tenure_days": 0,
                "ltv_tier": DEFAULT_LTV_TIER,
                "historical_ptp_honor_rate": DEFAULT_HISTORICAL_PTP_HONOR_RATE,
                "prior_retry_success_count": 0,
            },
            True,
        )

    today = date.today()
    now_iso = datetime.now(timezone.utc).isoformat()

    with _connect() as conn:
        row = conn.execute(
            "SELECT tenure_start_date, ltv_tier, historical_ptp_honor_rate, prior_retry_success_count "
            "FROM customer_history WHERE customer_key = ?",
            (customer_key,),
        ).fetchone()

        if row is not None:
            tenure_start_date, ltv_tier, historical_ptp_honor_rate, prior_retry_success_count = row
            tenure_days = (today - date.fromisoformat(tenure_start_date)).days
            return (
                {
                    "customer_tenure_days": max(tenure_days, 0),
                    "ltv_tier": ltv_tier,
                    "historical_ptp_honor_rate": historical_ptp_honor_rate,
                    "prior_retry_success_count": prior_retry_success_count,
                },
                False,
            )

        conn.execute(
            "INSERT INTO customer_history "
            "(customer_key, tenure_start_date, ltv_tier, historical_ptp_honor_rate, "
            "prior_retry_success_count, total_retry_outcomes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, 0, ?, ?)",
            (customer_key, today.isoformat(), DEFAULT_LTV_TIER, DEFAULT_HISTORICAL_PTP_HONOR_RATE, now_iso, now_iso),
        )
        return (
            {
                "customer_tenure_days": 0,
                "ltv_tier": DEFAULT_LTV_TIER,
                "historical_ptp_honor_rate": DEFAULT_HISTORICAL_PTP_HONOR_RATE,
                "prior_retry_success_count": 0,
            },
            True,
        )


def record_payment_outcome(customer_key: str | None, recovered: bool) -> None:
    """Updates prior_retry_success_count / historical_ptp_honor_rate for
    customer_key based on one resolved outcome -- call this from
    subscription.charged (recovered=True) and from a payment.failed /
    subscription.pending / subscription.halted event for a customer who has
    a prior record (recovered=False). historical_ptp_honor_rate is
    recomputed as prior_retry_success_count / total_retry_outcomes after
    this update (a plain running rate, not an exponential/weighted one --
    the volumes here are small enough that the distinction doesn't matter).

    No-op if customer_key is None (nothing to update) or if the customer has
    no existing record (get_or_create_customer should be called first to
    create one -- this function does not create records itself).
    """
    if not customer_key:
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT prior_retry_success_count, total_retry_outcomes FROM customer_history WHERE customer_key = ?",
            (customer_key,),
        ).fetchone()
        if row is None:
            return

        prior_retry_success_count, total_retry_outcomes = row
        total_retry_outcomes += 1
        if recovered:
            prior_retry_success_count += 1
        new_rate = prior_retry_success_count / total_retry_outcomes

        conn.execute(
            "UPDATE customer_history SET prior_retry_success_count = ?, total_retry_outcomes = ?, "
            "historical_ptp_honor_rate = ?, updated_at = ? WHERE customer_key = ?",
            (prior_retry_success_count, total_retry_outcomes, new_rate, now_iso, customer_key),
        )
