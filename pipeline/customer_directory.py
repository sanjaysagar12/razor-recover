"""
Email <-> customer_key directory (SQLite) -- Phase 18.

Real Razorpay payment.failed webhooks carry payload.payment.entity.email/
.contact (see webhook_receiver.map_payload_to_case), but until this phase
nothing persisted that anywhere: not WEBHOOK_AUDIT_COLUMNS, not
customer_history, not promises, not customer_ptp_stats. There was no
email-indexed lookup anywhere in the system. This module is that lookup.

Colocated with pipeline/customer_history.py and pipeline/promise_store.py in
the same database file (data/customer_history.db) -- same precedent
promise_store.py already set (a separate module/table for a separate
concern, not a new database technology). Deliberately a SEPARATE module
from customer_history.py rather than a new column/table bolted onto it:
customer_history.py is keyed by customer_key and holds SCORING features
(tenure, ltv_tier, honor rate, retry counts) read-modify-write on every case
for confidence_gate/shap_extract. An email directory is a pure identity
lookup with no relationship to scoring, and conflating the two would force
every future read of customer_history to filter out directory-only columns.

One customer_key can appear under more than one email over time (rare, but
possible if a customer's payment method email changes) and one email can
map to more than one customer_key (multiple Razorpay customer_id/
subscription records for the same person) -- so the table is keyed on the
(email, customer_key) pair, not on either alone, and get_customer_keys_for_
email() returns a list.

logger uses the "webhook_receiver" logger name, same as pipeline/
ptp_outcomes.py -- this module is only ever called from
webhook_receiver.map_payload_to_case(), so its log lines belong in that
same handler/file (logs/webhook_receiver.log), not a separate stream.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "customer_history.db"

logger = logging.getLogger("webhook_receiver")


@contextmanager
def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_directory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                contact TEXT,
                customer_key TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_directory_email_key "
            "ON customer_directory (email, customer_key)"
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_customer_contact(email: str | None, contact: str | None, customer_key: str | None) -> None:
    """Upserts one (email, customer_key) row -- called from
    map_payload_to_case() on EVERY webhook, not just the chat/promise-reply
    path, so the directory fills up from real webhook traffic automatically.

    No-op, logged, if email is None (many events won't carry one -- this is
    the expected common case, not an error) or if customer_key is None
    (nothing stable to map this email to -- see customer_history.py's own
    None-customer_key handling for the same reasoning). A first-seen pair
    sets both first_seen_at and last_seen_at to now; a repeat webhook for an
    already-known pair updates contact (a customer's phone number can change
    between events) and last_seen_at only, leaving first_seen_at untouched.
    """
    if not email:
        return
    if not customer_key:
        logger.info(
            "customer_directory: email=%s present but customer_key is None -- nothing stable to map it to, skipping",
            email,
        )
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM customer_directory WHERE email = ? AND customer_key = ?",
            (email, customer_key),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO customer_directory (email, contact, customer_key, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (email, contact, customer_key, now_iso, now_iso),
            )
            logger.info(
                "customer_directory: first-seen pair email=%s customer_key=%s", email, customer_key
            )
        else:
            conn.execute(
                "UPDATE customer_directory SET contact = ?, last_seen_at = ? WHERE email = ? AND customer_key = ?",
                (contact, now_iso, email, customer_key),
            )


def get_customer_keys_for_email(email: str) -> list[str]:
    """All customer_key values ever seen for email, most-recently-active
    first. Returns [] for an unknown email -- never raises."""
    if not email:
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT customer_key FROM customer_directory WHERE email = ? ORDER BY last_seen_at DESC",
            (email,),
        ).fetchall()
        return [row[0] for row in rows]


def reset_all() -> None:
    """Deletes every row from customer_directory -- backs the Customer
    Conversations page's reset button (webhook_receiver.api_reset_
    conversations). Only this table: customer_history's tenure/ltv/honor-
    rate scoring columns live in the SAME database file (data/
    customer_history.db) but are a separate table, untouched here -- that's
    pipeline scoring state, not conversation-page data. Callers that also
    want promise-reply threads cleared must call promise_store.
    reset_all_promises() separately (see api_reset_conversations, which
    calls both together)."""
    with _connect() as conn:
        conn.execute("DELETE FROM customer_directory")


def list_all_customers() -> list[dict]:
    """One row per known email -- customer_key holds every customer_key ever
    seen for that email (comma-joined, in case_count-relevant order isn't
    needed here; Phase 19's /api/customers computes case_count/last_activity
    itself from promise/audit data, this is purely the identity side).
    Ordered by most-recently-active email first."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT email,
                   GROUP_CONCAT(DISTINCT customer_key) AS customer_keys,
                   MIN(first_seen_at) AS first_seen_at,
                   MAX(last_seen_at) AS last_seen_at
            FROM customer_directory
            GROUP BY email
            ORDER BY MAX(last_seen_at) DESC
            """
        ).fetchall()
        return [
            {
                "email": row["email"],
                "customer_keys": row["customer_keys"].split(",") if row["customer_keys"] else [],
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
            }
            for row in rows
        ]
