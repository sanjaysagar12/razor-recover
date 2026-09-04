"""
Razorpay API client helpers.

TEST MODE ONLY. These functions are meant to be used with Razorpay test-mode
credentials (key IDs starting with `rzp_test_`). Do not point this at live
keys — create_test_plan()/create_test_subscription() create real objects via
the Razorpay API and are only safe against a test-mode account.
"""

import os

import razorpay
from dotenv import load_dotenv

load_dotenv()

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")


def get_client() -> razorpay.Client:
    """Initialize and return a Razorpay client from .env credentials.

    TEST MODE ONLY — expects RAZORPAY_KEY_ID to start with `rzp_test_`.
    """
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise RuntimeError(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set. "
            "Copy .env.example to .env and fill in your Razorpay test-mode keys."
        )
    if not RAZORPAY_KEY_ID.startswith("rzp_test_"):
        raise RuntimeError(
            "RAZORPAY_KEY_ID does not look like a test-mode key (expected it to "
            "start with 'rzp_test_'). Refusing to run against non-test keys."
        )

    client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    return client


def create_test_plan() -> dict:
    """Create a ₹500/month test-mode subscription plan via POST /v1/plans.

    TEST MODE ONLY. Returns the created plan object (includes its `id`).
    """
    client = get_client()
    plan = client.plan.create(
        {
            "period": "monthly",
            "interval": 1,
            "item": {
                "name": "Revenue Recovery Test Plan",
                "amount": 50000,  # amount is in paise: 500 INR
                "currency": "INR",
                "description": "Test plan for revenue recovery agent (Phase 1 smoke test)",
            },
        }
    )
    return plan


def create_test_subscription(plan_id: str) -> dict:
    """Create a test-mode subscription against `plan_id` via POST /v1/subscriptions.

    TEST MODE ONLY. Returns the created subscription object.
    """
    client = get_client()
    subscription = client.subscription.create(
        {
            "plan_id": plan_id,
            "total_count": 12,
            "quantity": 1,
        }
    )
    return subscription


def fetch_plan(plan_id: str) -> dict:
    """Fetch a plan by id via GET /v1/plans/:id.

    TEST MODE ONLY. Used as an end-to-end smoke test that credentials work.
    """
    client = get_client()
    return client.plan.fetch(plan_id)


def cancel_all_payment_links() -> dict:
    """Cancels every payment link currently in 'created' status (active,
    unpaid, not yet cancelled/expired/paid).

    Exists because test-mode Razorpay accounts cap active payment links at
    30 (see pipeline/execute_action.py's _send_payment_link /
    execute_promise_reschedule -- both surface "test mode limit of 30
    reached for payment_link" as execution_status="failed" once that cap is
    hit). This is the operator escape hatch: free up the quota by cancelling
    links this pipeline already created rather than doing it one by one in
    the Razorpay dashboard.

    Paginates client.payment_link.all() (100 per page, Razorpay's max
    count) rather than relying on a default page size. Only attempts
    cancel() on status=='created' -- paid/cancelled/expired links are
    already terminal and client.payment_link.cancel() would just error on
    them for no benefit. Each cancel is wrapped individually so one failure
    (already cancelled between the list and cancel calls, network blip)
    never aborts the rest of the batch.

    Returns {"total_active": int, "cancelled": [ids], "failed": [{"id", "error"}]}.
    """
    client = get_client()
    active_ids = []
    skip = 0
    page_size = 100
    while True:
        page = client.payment_link.all({"count": page_size, "skip": skip})
        # The SDK's payment_link.all() nests results under "payment_links",
        # not "items" (unlike client.payment_link.create()'s own return
        # shape, or other resources like client.order.all()) -- verified
        # against a live response; reading "items" here silently returned []
        # every time, so cancel_all_payment_links always reported
        # total_active=0 regardless of how many links actually existed.
        links = page.get("payment_links", [])
        active_ids.extend(link["id"] for link in links if link.get("status") == "created")
        if len(links) < page_size:
            break
        skip += page_size

    cancelled = []
    failed = []
    for link_id in active_ids:
        try:
            client.payment_link.cancel(link_id)
            cancelled.append(link_id)
        except Exception as exc:  # noqa: BLE001 -- one bad link must not abort the batch
            failed.append({"id": link_id, "error": f"{type(exc).__name__}: {exc}"})

    return {"total_active": len(active_ids), "cancelled": cancelled, "failed": failed}
