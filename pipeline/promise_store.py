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
# Phase 15 -- the clarification loop gave up after MAX_CLARIFICATION_ROUNDS
# unresolved replies and used a fallback schedule instead of a customer-
# confirmed date. Distinct from OUTCOME_AMBIGUOUS, which just means "awaiting
# a clarification question" -- OUTCOME_NO_REPLY means the loop is over.
OUTCOME_NO_REPLY = "no_reply"
# Phase 16 -- terminal honor/break states for a promise that WAS scheduled
# (has a payment_link_id, outcome was 'pending'). Set by pipeline/
# ptp_outcomes.py, never directly by the reply-intake or reschedule-execution
# flows above.
OUTCOME_HONORED = "honored"
OUTCOME_BROKEN = "broken"

# Phase 15 -- clarification-loop lifecycle. Every promise row's `status`
# holds one of these, independent of `outcome` above (outcome is Phase 9-14's
# "how did the raw reply resolve" field; status is Phase 15's "where is this
# reply in the ask-again-or-give-up loop" field).
STATUS_PENDING = "pending"
STATUS_CLARIFYING = "clarifying"
STATUS_SCHEDULED = "scheduled"
STATUS_FALLBACK = "fallback"
STATUS_REQUIRES_HUMAN_REVIEW = "requires_human_review"

# Cap on how many times a customer is re-asked to clarify an ambiguous/
# low-confidence reply before the pipeline stops asking and falls back
# automatically -- see webhook_receiver.py's clarification-loop handler.
MAX_CLARIFICATION_ROUNDS = 2


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
                resolved_at TEXT,
                clarification_round INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                late_recovery_at TEXT
            )
            """
        )
        _migrate_clarification_columns(conn)
        _migrate_honor_tracking_columns(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_clarification_columns(conn: sqlite3.Connection) -> None:
    """Phase 15 -- adds clarification_round/status to a promises table that
    may already exist from Phase 9-14 (SQLite has no 'ADD COLUMN IF NOT
    EXISTS', so check PRAGMA table_info first; a table just created fresh by
    CREATE TABLE IF NOT EXISTS above already has both columns and this is a
    no-op). Existing rows backfill status from payment_link_id -- the
    closest existing proxy for "was this promise actually scheduled", since
    outcome alone never records a success state (see
    update_promise_payment_link's docstring: outcome stays 'pending' even
    after a successful reschedule). clarification_round backfills to 0 for
    every pre-existing row, since no round history existed before this
    phase."""
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(promises)").fetchall()}
    if "clarification_round" not in existing_columns:
        conn.execute("ALTER TABLE promises ADD COLUMN clarification_round INTEGER NOT NULL DEFAULT 0")
    if "status" not in existing_columns:
        conn.execute("ALTER TABLE promises ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
        conn.execute(
            "UPDATE promises SET status = CASE WHEN payment_link_id IS NOT NULL THEN ? ELSE ? END",
            (STATUS_SCHEDULED, STATUS_PENDING),
        )


def _migrate_honor_tracking_columns(conn: sqlite3.Connection) -> None:
    """Phase 16 -- adds late_recovery_at to a promises table that may already
    exist from Phase 9-15 (a table just created fresh by CREATE TABLE IF NOT
    EXISTS above already has it and this is a no-op). No backfill needed --
    a late recovery can only be observed going forward, from a webhook event
    that arrives after this phase is deployed."""
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(promises)").fetchall()}
    if "late_recovery_at" not in existing_columns:
        conn.execute("ALTER TABLE promises ADD COLUMN late_recovery_at TEXT")


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


def get_latest_promise_for_case(case_id: str, exclude_promise_id: str | None = None) -> dict | None:
    """Fetches the most recently created promise row for case_id, or None if
    this is the case's first reply. exclude_promise_id skips the row just
    inserted for the CURRENT reply (create_promise runs before extraction,
    so by the time the clarification loop needs "the prior round", the
    current reply's own row already exists in the table).

    Each customer reply gets its own fresh promise row (see create_promise)
    -- there is no single running row per case_id -- so this is how Phase
    15's clarification loop finds the running clarification_round for a
    case across multiple replies."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        if exclude_promise_id:
            row = conn.execute(
                "SELECT * FROM promises WHERE case_id = ? AND promise_id != ? "
                "ORDER BY created_at DESC LIMIT 1",
                (case_id, exclude_promise_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM promises WHERE case_id = ? ORDER BY created_at DESC LIMIT 1",
                (case_id,),
            ).fetchone()
        return dict(row) if row else None


def update_promise_clarification(promise_id: str, clarification_round: int, status: str) -> None:
    """Writes Phase 15's clarification-loop state back onto the row.
    clarification_round and status are always written together (never
    independently) so the two fields can't drift out of sync -- e.g.
    round=1 while status is still 'pending'."""
    with _connect() as conn:
        conn.execute(
            "UPDATE promises SET clarification_round = ?, status = ? WHERE promise_id = ?",
            (clarification_round, status, promise_id),
        )


def update_promise_status(promise_id: str, status: str) -> None:
    """Standalone status update for paths that don't also touch
    clarification_round -- e.g. the non-ambiguous path moving to 'scheduled'
    on a successful reschedule, or to 'requires_human_review' when a
    guardrail rule (window_cap_check) flags requires_human_review=True."""
    with _connect() as conn:
        conn.execute("UPDATE promises SET status = ? WHERE promise_id = ?", (status, promise_id))


def mark_promise_no_reply(promise_id: str) -> None:
    """Phase 15 fallback path -- the customer never resolved an ambiguous
    reply within MAX_CLARIFICATION_ROUNDS, so outcome moves to 'no_reply'.
    Distinct from 'ambiguous' (still awaiting a clarification question) --
    'no_reply' means the loop is over and a fallback schedule was used
    instead of a customer-confirmed date."""
    with _connect() as conn:
        conn.execute(
            "UPDATE promises SET outcome = ? WHERE promise_id = ?",
            (OUTCOME_NO_REPLY, promise_id),
        )


# --------------------------------------------------------------------------
# Phase 16 -- honor/break tracking. Query and terminal-state helpers used by
# pipeline/ptp_outcomes.py; this module stays pure DB access (no webhook
# payload parsing, no CSV logging, no stats -- see that module's docstring
# for why it lives separately).
# --------------------------------------------------------------------------
def get_promise_by_payment_link_id(payment_link_id: str) -> dict | None:
    """Fallback match for find_open_promise/find_any_promise when a webhook
    payload's notes are missing or don't carry promise_id -- looks up by the
    payment_link_id stored on the row by update_promise_payment_link. Returns
    the most recently created match if (improbably) more than one promise
    row ever shares a payment_link_id; in practice each Phase 14 reschedule
    creates a fresh Razorpay payment link, so this is one-to-one."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM promises WHERE payment_link_id = ? ORDER BY created_at DESC LIMIT 1",
            (payment_link_id,),
        ).fetchone()
        return dict(row) if row else None


def get_recent_resolved_outcomes(customer_id: str | None, limit: int = 2) -> list[str]:
    """Most-recent-first outcomes ('honored'/'broken') for customer_id's last
    `limit` RESOLVED promises (ordered by resolved_at DESC) -- Phase 17 uses
    this to detect two CONSECUTIVE broken promises for the watch->restricted
    risk-tier transition, as distinct from customer_ptp_stats' aggregate
    historical_ptp_honor_rate (which can't tell "broken, then broken" apart
    from "broken, then honored, then broken").

    late_recovery_at is irrelevant here: a promise that recovered late is
    still outcome='broken' (see mark_promise_late_recovery's docstring --
    outcome deliberately never flips back), so it correctly still counts as
    a broken outcome for this consecutive check.

    Returns [] for customer_id=None (nothing to key the query on) or a
    customer with no resolved promises yet.
    """
    if not customer_id:
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT outcome FROM promises WHERE customer_id = ? AND outcome IN (?, ?) "
            "ORDER BY resolved_at DESC LIMIT ?",
            (customer_id, OUTCOME_HONORED, OUTCOME_BROKEN, limit),
        ).fetchall()
        return [row[0] for row in rows]


def get_expired_pending_promises(today_iso: str) -> list[dict]:
    """Rows eligible for check_expired_promises() to mark 'broken':
    outcome='pending' (never yet resolved to honored/broken), extracted_date
    in the past relative to today_iso (YYYY-MM-DD, string comparison is
    valid since extracted_date is always YYYY-MM-DD -- see llm_layer's
    _ISO_DATE_RE validation), AND payment_link_id IS NOT NULL.

    That last condition isn't in the plan doc's literal SQL sketch, but is
    required for correctness: outcome stays 'pending' not just for scheduled
    promises awaiting resolution, but also for ones a PTP guardrail rejected
    (rejected_window_cap/rejected_past_date) or that are still mid
    clarification -- those never got a payment link and must never be
    reported as a 'broken' promise-to-pay, since no payment was ever
    scheduled for them to break."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM promises WHERE outcome = ? AND payment_link_id IS NOT NULL "
            "AND extracted_date IS NOT NULL AND extracted_date < ?",
            (OUTCOME_PENDING, today_iso),
        ).fetchall()
        return [dict(row) for row in rows]


def mark_promise_honored(promise_id: str) -> None:
    """outcome 'pending' -> 'honored', resolved_at = now(). Called when a
    payment.captured/payment_link.paid webhook matches a still-open
    promise."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE promises SET outcome = ?, resolved_at = ? WHERE promise_id = ?",
            (OUTCOME_HONORED, now_iso, promise_id),
        )


def mark_promise_broken(promise_id: str) -> None:
    """outcome 'pending' -> 'broken', resolved_at = now(). Called only by
    check_expired_promises() -- never directly from a payment.failed webhook
    (see that handler's docstring: a customer may still pay before the
    deadline)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE promises SET outcome = ?, resolved_at = ? WHERE promise_id = ?",
            (OUTCOME_BROKEN, now_iso, promise_id),
        )


def mark_promise_late_recovery(promise_id: str) -> None:
    """Records that money eventually arrived for a promise the deadline
    check already marked 'broken' -- late_recovery_at is set, but outcome
    deliberately stays 'broken' (the promise itself was broken; the debt
    being recovered late is a separate fact, tracked here so it isn't lost,
    not a reason to rewrite history). See pipeline/ptp_outcomes.py's
    handle_payment_captured for the routing decision."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE promises SET late_recovery_at = ? WHERE promise_id = ?",
            (now_iso, promise_id),
        )
