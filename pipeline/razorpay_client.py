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
