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

--------------------------------------------------------------------------
Phase 17 -- feedback loop / escalation ladder (current_risk_tier).
--------------------------------------------------------------------------
Adds current_risk_tier ('normal' | 'watch' | 'restricted', default
'normal') to this same row -- extending the existing per-customer store
rather than adding a new one. Recomputed by compute_risk_tier (a pure
function, no DB access) every time record_promise_resolution runs, i.e.
hooked into the same Phase 16 webhook-resolution path, not a separate
cron/poll job.

See guardrails.py's customer_risk_restricted rule (consumes
current_risk_tier to force a restricted customer's case straight to
requires_human_review, bypassing the self-service PTP chat/reschedule
flow) and confidence_gate.route_case's risk_tier parameter (a 'watch'
customer routes to the LLM layer even when the tree score alone
wouldn't).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import promise_store

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "customer_history.db"

RISK_TIER_NORMAL = "normal"
RISK_TIER_WATCH = "watch"
RISK_TIER_RESTRICTED = "restricted"

# normal -> watch trigger thresholds.
WATCH_HONOR_RATE_THRESHOLD = 0.5
WATCH_MIN_PROMISES_MADE = 2

# restricted never auto-clears -- not even on a subsequent honored promise
# (see compute_risk_tier). Per the Phase 17 brief this tier is meant to
# require an explicit human review before a customer can be reset back to
# 'normal'/'watch'. No such admin action/endpoint exists yet in this repo;
# TODO(phase17-restricted-reset): build one (a manual API endpoint or a
# reviewed dashboard action) before 'restricted' can ever be lifted for a
# real customer. Until then this tier is a one-way door by design, not an
# oversight.
TODO_RESTRICTED_RESET_REQUIRES_HUMAN_REVIEW = True


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
                current_risk_tier TEXT NOT NULL DEFAULT 'normal',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        _migrate_risk_tier_column(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_risk_tier_column(conn: sqlite3.Connection) -> None:
    """Phase 17 -- adds current_risk_tier to a customer_ptp_stats table that
    may already exist from Phase 16 (SQLite has no 'ADD COLUMN IF NOT
    EXISTS', so check PRAGMA table_info first; a table just created fresh by
    CREATE TABLE IF NOT EXISTS above already has the column and this is a
    no-op). Existing rows backfill to 'normal' -- the correct default for
    "we have honor-rate history but never evaluated a tier for this customer
    before", not a guess at their real risk."""
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(customer_ptp_stats)").fetchall()}
    if "current_risk_tier" not in existing_columns:
        conn.execute(
            f"ALTER TABLE customer_ptp_stats ADD COLUMN current_risk_tier TEXT NOT NULL DEFAULT '{RISK_TIER_NORMAL}'"
        )


# --------------------------------------------------------------------------
# Phase 17 -- tier transition. Pure function: no DB access, no side effects
# beyond computing and returning the next tier. record_promise_resolution
# below is the only caller that persists its result.
# --------------------------------------------------------------------------
def compute_risk_tier(
    current_tier: str,
    promises_made: int,
    historical_ptp_honor_rate: float,
    recent_outcomes: list[str],
) -> str:
    """Next current_risk_tier value for one customer, given their state AFTER
    the promise resolution that just happened (promises_made/
    historical_ptp_honor_rate already include it; recent_outcomes[0] is that
    same resolution's outcome, most-recent-first, from
    promise_store.get_recent_resolved_outcomes).

    Transition rules (see module docstring for the fuller Phase 17 spec):
      restricted -> restricted always. No promise outcome, however good,
        auto-clears it -- see TODO_RESTRICTED_RESET_REQUIRES_HUMAN_REVIEW.
      normal -> watch when historical_ptp_honor_rate < WATCH_HONOR_RATE_THRESHOLD
        AND promises_made >= WATCH_MIN_PROMISES_MADE.
      normal -> restricted (skipping a separate watch-tenure event) when the
        above ALSO coincides with the two most recent promises both being
        broken. This isn't a shortcut around "watch must come first" -- with
        exactly 2 total promises, a rate < 0.5 is only reachable if both
        broke, so "just became watch-eligible" and "watch's own escalation
        condition" are the same fact observed at the same moment. There is
        no real intermediate milestone to stop at.
      watch -> restricted on a second CONSECUTIVE broken promise -- the two
        most recent resolved promises for this customer both 'broken', not
        merely the overall rate dipping further.
      watch -> normal (recovery) when the tier coming INTO this call was
        already 'watch' and the most recent promise was honored. Recovery
        only applies to a customer who was already sitting in 'watch' before
        this event -- it never fires for a customer promoted to 'watch' by
        this same resolution (that promotion path is handled above, and by
        construction that path's most recent outcome is always 'broken', so
        the two cases never actually collide).
    """
    if current_tier == RISK_TIER_RESTRICTED:
        return RISK_TIER_RESTRICTED

    two_consecutive_broken = (
        len(recent_outcomes) >= 2
        and recent_outcomes[0] == promise_store.OUTCOME_BROKEN
        and recent_outcomes[1] == promise_store.OUTCOME_BROKEN
    )
    would_enter_watch = (
        historical_ptp_honor_rate < WATCH_HONOR_RATE_THRESHOLD and promises_made >= WATCH_MIN_PROMISES_MADE
    )

    if current_tier == RISK_TIER_NORMAL:
        if would_enter_watch and two_consecutive_broken:
            return RISK_TIER_RESTRICTED
        return RISK_TIER_WATCH if would_enter_watch else RISK_TIER_NORMAL

    if current_tier == RISK_TIER_WATCH:
        if two_consecutive_broken:
            return RISK_TIER_RESTRICTED
        if recent_outcomes and recent_outcomes[0] == promise_store.OUTCOME_HONORED:
            return RISK_TIER_NORMAL
        return RISK_TIER_WATCH

    # Defensive fallback for an unrecognized stored value -- never crash the
    # webhook resolution path over a tier string, just leave it as-is.
    return current_tier


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

    Phase 17 -- current_risk_tier is recomputed (via compute_risk_tier) and
    persisted in this same upsert, using promise_store.get_recent_resolved_outcomes
    for the two-most-recent-outcomes input. This function's caller
    (pipeline/ptp_outcomes.py) always calls promise_store.mark_promise_honored/
    broken BEFORE calling here, so that lookup already reflects the
    resolution this very call is recording -- recent_outcomes[0] below is
    always this resolution's own outcome, not a stale one.

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
            "SELECT promises_made, promises_honored, current_risk_tier FROM customer_ptp_stats WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()

        current_tier = row["current_risk_tier"] if row is not None else RISK_TIER_NORMAL
        recent_outcomes = promise_store.get_recent_resolved_outcomes(customer_id, limit=2)

        if row is None:
            promises_made = 1
            promises_honored = 1 if honored else 0
            new_rate = promises_honored / promises_made
            new_tier = compute_risk_tier(current_tier, promises_made, new_rate, recent_outcomes)
            conn.execute(
                "INSERT INTO customer_ptp_stats "
                "(customer_id, promises_made, promises_honored, historical_ptp_honor_rate, "
                "current_risk_tier, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (customer_id, promises_made, promises_honored, new_rate, new_tier, now_iso, now_iso),
            )
        else:
            promises_made = row["promises_made"] + 1
            promises_honored = row["promises_honored"] + (1 if honored else 0)
            new_rate = promises_honored / promises_made
            new_tier = compute_risk_tier(current_tier, promises_made, new_rate, recent_outcomes)
            conn.execute(
                "UPDATE customer_ptp_stats SET promises_made = ?, promises_honored = ?, "
                "historical_ptp_honor_rate = ?, current_risk_tier = ?, updated_at = ? WHERE customer_id = ?",
                (promises_made, promises_honored, new_rate, new_tier, now_iso, customer_id),
            )

        return {
            "customer_id": customer_id,
            "promises_made": promises_made,
            "promises_honored": promises_honored,
            "historical_ptp_honor_rate": new_rate,
            "current_risk_tier": new_tier,
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


def get_risk_tier(customer_id: str | None) -> str:
    """Convenience reader for the two Phase 17 integration points
    (guardrails.py, confidence_gate.py) -- returns RISK_TIER_NORMAL for
    customer_id=None or a customer with no resolved-promise history yet,
    same "unseen means neutral, not risky" default customer_history.py
    already uses for historical_ptp_honor_rate."""
    stats = get_stats(customer_id)
    return stats["current_risk_tier"] if stats else RISK_TIER_NORMAL
