"""
Promise-to-pay (PTP) reply store (SQLite) -- Phase 11, reply intake channel.

Colocated with pipeline/customer_history.py in the same database file
(data/customer_history.db) -- that's the project's one existing SQLite
mechanism, no new database technology introduced. A separate table,
`promises`, keyed by a generated promise_id (not customer_key), one row per
customer reply to a "when can you pay?" prompt.

This module only stores the RAW reply. Nothing here parses a date, applies
guardrails, or creates a payment link -- those are later phases.
extracted_date, extraction_confidence, guardrail_status, and
payment_link_id are always NULL coming out of this module; outcome starts
at "pending" and is only moved forward by later phases.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "customer_history.db"

OUTCOME_PENDING = "pending"
OUTCOME_AMBIGUOUS = "ambiguous"
OUTCOME_RESCHEDULE_FAILED = "reschedule_failed"


@contextmanager
def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promises (
                promise_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                customer_id TEXT,
                raw_customer_reply TEXT NOT NULL,
                extracted_date TEXT,
                extraction_confidence REAL,
                guardrail_status TEXT,
                payment_link_id TEXT,
                outcome TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                resolved_at TEXT
            )
            """
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_promise(case_id: str, customer_id: str | None, raw_customer_reply: str) -> str:
    """Inserts a new pending promise row and returns its generated
    promise_id. raw_customer_reply is stored exactly as given -- no
    trimming or content rewriting, only sqlite3's normal parameter binding
    (safe against injection, not a semantic transform). Callers must insert
    this row before doing anything else with the reply, so the raw message
    is durable even if later processing (not implemented yet) fails.
    """
    promise_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO promises "
            "(promise_id, case_id, customer_id, raw_customer_reply, extracted_date, "
            "extraction_confidence, guardrail_status, payment_link_id, outcome, created_at, resolved_at) "
            "VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, NULL)",
            (promise_id, case_id, customer_id, raw_customer_reply, OUTCOME_PENDING, now_iso),
        )
    return promise_id


def update_promise_extraction(
    promise_id: str, extracted_date: str | None, extraction_confidence: float | None, ambiguous: bool
) -> None:
    """Writes a date-extraction result back onto the row create_promise
    already inserted -- update in place, never a new row/table.
    guardrail_status and payment_link_id are untouched here (still NULL,
    same as create_promise left them -- guardrail validation and payment-
    link creation are later phases).

    outcome moves from 'pending' to 'ambiguous' when the extraction could
    not pull a specific date, so a later phase can find "replies awaiting a
    clarification question" without re-deriving that from extracted_date
    being NULL. A successful extraction leaves outcome at 'pending' --
    it still awaits guardrail validation before anything is actioned.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE promises SET extracted_date = ?, extraction_confidence = ?, outcome = ? WHERE promise_id = ?",
            (
                extracted_date,
                extraction_confidence,
                OUTCOME_AMBIGUOUS if ambiguous else OUTCOME_PENDING,
                promise_id,
            ),
        )


def get_promise(promise_id: str) -> dict | None:
    """Fetches one promises row as a plain dict, or None if promise_id
    doesn't exist -- used by Phase 14's reschedule execution flow (and its
    tests) to read back a promise record rather than re-threading every
    field through function arguments by hand."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM promises WHERE promise_id = ?", (promise_id,)).fetchone()
        return dict(row) if row else None


def update_promise_guardrail(promise_id: str, guardrail_status: str) -> None:
    """Writes guardrails.apply_ptp_guardrails' verdict back onto the row --
    called regardless of the verdict (approved, rejected_*, adjusted,
    pending_clarification) so a non-approved promise's guardrail_status is
    visible on the row without re-running guardrails to find out why it
    wasn't scheduled."""
    with _connect() as conn:
        conn.execute(
            "UPDATE promises SET guardrail_status = ? WHERE promise_id = ?",
            (guardrail_status, promise_id),
        )


def update_promise_payment_link(promise_id: str, payment_link_id: str) -> None:
    """Phase 14 success path -- stores the Razorpay payment_link_id created
    for this promise. outcome is deliberately left untouched (stays
    'pending' -- a later phase moves it to honored/broken from webhook
    events, not from the act of scheduling itself)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE promises SET payment_link_id = ? WHERE promise_id = ?",
            (payment_link_id, promise_id),
        )


def mark_promise_reschedule_failed(promise_id: str) -> None:
    """Phase 14 failure path -- a Razorpay API/network failure must never be
    silently recorded as a scheduled promise. payment_link_id stays NULL;
    outcome moves to 'reschedule_failed' so a retry/backfill job can find
    these directly instead of re-deriving the failure from an empty
    payment_link_id on an otherwise-'pending' row."""
    with _connect() as conn:
        conn.execute(
            "UPDATE promises SET outcome = ? WHERE promise_id = ?",
            (OUTCOME_RESCHEDULE_FAILED, promise_id),
        )
