"""
Local smoke test for webhook_receiver.py.

Posts a Razorpay-documented-shape `payment.failed` sample payload to
http://localhost:5555/webhook/razorpay with a correctly-computed
X-Razorpay-Signature header (HMAC-SHA256 of the raw body, same secret the
receiver reads from .env), so signature verification and the full pipeline
call can be checked before touching the real Razorpay dashboard.

Run (with webhook_receiver.py already running in another terminal):
    python test_webhook_locally.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL = "http://localhost:5555/webhook/razorpay"
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

# Shape matches Razorpay's documented payment.failed webhook payload
# (https://razorpay.com/docs/webhooks/payloads/payments/#payment-failed).
SAMPLE_PAYMENT_FAILED = {
    "entity": "event",
    "account_id": "acc_test000000000001",
    "event": "payment.failed",
    "contains": ["payment"],
    "payload": {
        "payment": {
            "entity": {
                "id": f"pay_test_{int(time.time())}",
                "entity": "payment",
                "amount": 66242,  # paise -> Rs 662.42
                "currency": "INR",
                "status": "failed",
                "order_id": "order_test000000000001",
                "invoice_id": None,
                "international": False,
                "method": "upi",
                "amount_refunded": 0,
                "refund_status": None,
                "captured": False,
                "description": "Recovery pipeline local smoke test",
                "card_id": None,
                "bank": None,
                "wallet": None,
                "vpa": "test@upi",
                "email": "test.customer@example.com",
                "contact": "+919999999999",
                "customer_id": "cust_test000000000001",
                "notes": [],
                "fee": None,
                "tax": None,
                "error_code": "BAD_REQUEST_ERROR",
                "error_description": "Payment failed due to issuer unavailability",
                "error_source": "issuer",
                "error_step": "payment_authorization",
                "error_reason": "issuer_unavailable",
                "created_at": int(time.time()),
            }
        }
    },
    "created_at": int(time.time()),
}


def compute_signature(raw_body: bytes, secret: str) -> str:
    return hmac.new(key=secret.encode("utf-8"), msg=raw_body, digestmod=hashlib.sha256).hexdigest()


def post_event(payload: dict, secret: str | None = None, corrupt_signature: bool = False) -> requests.Response:
    raw_body = json.dumps(payload).encode("utf-8")
    secret = secret if secret is not None else RAZORPAY_WEBHOOK_SECRET
    signature = compute_signature(raw_body, secret)
    if corrupt_signature:
        signature = "0" * len(signature)
    headers = {"Content-Type": "application/json", "X-Razorpay-Signature": signature}
    return requests.post(WEBHOOK_URL, data=raw_body, headers=headers, timeout=30)


def main() -> None:
    if not RAZORPAY_WEBHOOK_SECRET:
        print("RAZORPAY_WEBHOOK_SECRET not set in .env -- cannot compute a valid signature.")
        sys.exit(1)

    print(f"POST {WEBHOOK_URL}  event=payment.failed")
    print("--- Test 1: valid signature ---")
    resp = post_event(SAMPLE_PAYMENT_FAILED)
    print(f"status={resp.status_code}")
    try:
        print(json.dumps(resp.json(), indent=2))
    except ValueError:
        print(resp.text)

    print("\n--- Test 2: invalid signature (expect 400) ---")
    resp_bad = post_event(SAMPLE_PAYMENT_FAILED, corrupt_signature=True)
    print(f"status={resp_bad.status_code}")
    try:
        print(json.dumps(resp_bad.json(), indent=2))
    except ValueError:
        print(resp_bad.text)

    print("\n--- Test 3: subscription.charged (recovered case) ---")
    charged_payload = {
        "entity": "event",
        "event": "subscription.charged",
        "contains": ["payment", "subscription"],
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_test_{int(time.time())}_charged",
                    "amount": 50000,
                    "method": "upi",
                    "customer_id": "cust_test000000000001",
                    "created_at": int(time.time()),
                }
            },
            "subscription": {
                "entity": {
                    "id": "sub_test000000000001",
                    "paid_count": 2,
                    "customer_id": "cust_test000000000001",
                    "created_at": int(time.time()),
                }
            },
        },
        "created_at": int(time.time()),
    }
    resp_charged = post_event(charged_payload)
    print(f"status={resp_charged.status_code}")
    try:
        print(json.dumps(resp_charged.json(), indent=2))
    except ValueError:
        print(resp_charged.text)

    print("\n--- Test 4: unhandled event (expect status=ignored) ---")
    other_payload = {"entity": "event", "event": "payment.captured", "payload": {}, "created_at": int(time.time())}
    resp_other = post_event(other_payload)
    print(f"status={resp_other.status_code}")
    try:
        print(json.dumps(resp_other.json(), indent=2))
    except ValueError:
        print(resp_other.text)


if __name__ == "__main__":
    main()
