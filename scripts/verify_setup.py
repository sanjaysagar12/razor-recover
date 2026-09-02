"""
Phase 1 exit check.

Confirms the local environment and Razorpay TEST MODE credentials are wired
up correctly end to end: creates a test plan, then fetches it back.

Run with:
    python scripts/verify_setup.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from pipeline.razorpay_client import create_test_plan, fetch_plan

load_dotenv()


def main() -> int:
    print("Revenue Recovery — Phase 1 setup verification")
    print("-" * 50)

    try:
        print("[1/2] Creating test plan (POST /v1/plans) ...")
        plan = create_test_plan()
        plan_id = plan["id"]
        print(f"      OK — created plan {plan_id}")

        print("[2/2] Fetching plan back (GET /v1/plans/:id) ...")
        fetched = fetch_plan(plan_id)
        if fetched["id"] != plan_id:
            raise RuntimeError("Fetched plan id does not match created plan id")
        print(f"      OK — fetched plan {fetched['id']} "
              f"({fetched['item']['amount']} {fetched['item']['currency']} / "
              f"{fetched['period']})")

    except Exception as exc:  # noqa: BLE001 - top-level CLI error boundary
        print("-" * 50)
        print(f"FAILURE: {exc}")
        print("\nCheck that .env exists (copy from .env.example) and contains")
        print("valid Razorpay TEST MODE keys (RAZORPAY_KEY_ID starting with rzp_test_).")
        return 1

    print("-" * 50)
    print("SUCCESS: Razorpay test-mode credentials are working end to end.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
