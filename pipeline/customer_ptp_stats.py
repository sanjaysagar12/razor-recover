"""
Customer promise-to-pay (PTP) honor/break stats -- Phase 16.

Colocated in data/customer_history.db, same file pipeline/customer_history.py
and pipeline/promise_store.py already use -- the project's one existing
SQLite mechanism (see promise_store.py's module docstring), no new database
technology introduced here either. A separate table, `customer_ptp_stats`,
keyed by customer_id (the raw id promises.customer_id stores -- the value
the customer supplied at /api/promise-reply intake, NOT customer_history's
customer_key fallback-to-subscription scheme).

One row per customer, upserted every time a promise resolves to honored or
broken (never on pending/ambiguous/no_reply/reschedule_failed -- those
haven't resolved yet, and a late recovery on an already-broken promise
doesn't re-resolve it either, see pipeline/ptp_outcomes.py).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "customer_history.db"


@contextmanager
def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_ptp_stats (
                customer_id TEXT PRIMARY KEY,
                promises_made INTEGER NOT NULL DEFAULT 0,
                promises_honored INTEGER NOT NULL DEFAULT 0,
                historical_ptp_honor_rate REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_promise_resolution(customer_id: str | None, honored: bool) -> dict | None:
    """Upserts customer_id's row for one resolved promise (honored or
    broken -- callers must only invoke this once per promise resolution,
    never for a late recovery on an already-broken promise, which is a
    separate fact tracked on the promise row itself, not a second
    resolution here).

    promises_made always increments by 1; promises_honored increments only
    when honored=True. historical_ptp_honor_rate is recomputed as
    promises_honored / promises_made after the update -- a plain running
    rate, same convention pipeline/customer_history.record_payment_outcome
    already uses for its own honor-rate field.

    Returns the updated row as a dict, or None if customer_id is falsy
    (nothing to key the row on -- caller should log this as a gap, not
    silently lose the resolution).
    """
    if not customer_id:
        return None

    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT promises_made, promises_honored FROM customer_ptp_stats WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()

        if row is None:
            promises_made = 1
            promises_honored = 1 if honored else 0
            new_rate = promises_honored / promises_made
            conn.execute(
                "INSERT INTO customer_ptp_stats "
                "(customer_id, promises_made, promises_honored, historical_ptp_honor_rate, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (customer_id, promises_made, promises_honored, new_rate, now_iso, now_iso),
            )
        else:
            promises_made = row["promises_made"] + 1
            promises_honored = row["promises_honored"] + (1 if honored else 0)
            new_rate = promises_honored / promises_made
            conn.execute(
                "UPDATE customer_ptp_stats SET promises_made = ?, promises_honored = ?, "
                "historical_ptp_honor_rate = ?, updated_at = ? WHERE customer_id = ?",
                (promises_made, promises_honored, new_rate, now_iso, customer_id),
            )

        return {
            "customer_id": customer_id,
            "promises_made": promises_made,
            "promises_honored": promises_honored,
            "historical_ptp_honor_rate": new_rate,
        }


def get_stats(customer_id: str | None) -> dict | None:
    """Reads back customer_id's current stats row, or None if it doesn't
    exist yet (no resolved promise recorded for this customer) or
    customer_id is falsy."""
    if not customer_id:
        return None
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM customer_ptp_stats WHERE customer_id = ?", (customer_id,)
        ).fetchone()
        return dict(row) if row else None
