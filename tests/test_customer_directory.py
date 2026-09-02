"""
Phase 18 tests -- email <-> customer_key directory.

Plain-assert tests against the real local SQLite DB, same pattern
test_risk_tier.py/test_ptp_honor_break.py already use (unique uuid-suffixed
emails/customer_keys per test so runs never collide with each other or with
real webhook traffic already in data/customer_history.db).

Run with:
    python tests/test_customer_directory.py
or via pytest:
    python -m pytest tests/test_customer_directory.py -v
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import customer_directory


def _new_email(label: str) -> str:
    return f"test_{label}_{uuid.uuid4().hex[:8]}@example.com"


def test_email_none_is_a_noop():
    customer_directory.record_customer_contact(None, "9999999999", "cust_x")
    assert customer_directory.get_customer_keys_for_email(None) == []
    print("PASS  test_email_none_is_a_noop")


def test_customer_key_none_is_a_noop():
    email = _new_email("no_key")
    customer_directory.record_customer_contact(email, "9999999999", None)
    assert customer_directory.get_customer_keys_for_email(email) == []
    print("PASS  test_customer_key_none_is_a_noop")


def test_first_seen_pair_is_recorded_and_retrievable():
    email = _new_email("first_seen")
    customer_directory.record_customer_contact(email, "9000000001", "cust_a")
    keys = customer_directory.get_customer_keys_for_email(email)
    assert keys == ["cust_a"], keys
    print("PASS  test_first_seen_pair_is_recorded_and_retrievable")


def test_repeat_webhook_same_pair_does_not_duplicate():
    email = _new_email("repeat")
    customer_directory.record_customer_contact(email, "9000000002", "cust_b")
    customer_directory.record_customer_contact(email, "9000000003", "cust_b")  # contact changed
    keys = customer_directory.get_customer_keys_for_email(email)
    assert keys == ["cust_b"], keys  # no duplicate row for the same (email, customer_key)
    print("PASS  test_repeat_webhook_same_pair_does_not_duplicate")


def test_one_email_can_map_to_multiple_customer_keys():
    email = _new_email("multi")
    customer_directory.record_customer_contact(email, "9000000004", "cust_c1")
    customer_directory.record_customer_contact(email, "9000000004", "cust_c2")
    keys = set(customer_directory.get_customer_keys_for_email(email))
    assert keys == {"cust_c1", "cust_c2"}, keys
    print("PASS  test_one_email_can_map_to_multiple_customer_keys")


def test_list_all_customers_includes_recorded_email():
    email = _new_email("listed")
    customer_directory.record_customer_contact(email, "9000000005", "cust_d")
    all_customers = customer_directory.list_all_customers()
    matching = [row for row in all_customers if row["email"] == email]
    assert len(matching) == 1, matching
    assert matching[0]["customer_keys"] == ["cust_d"]
    print("PASS  test_list_all_customers_includes_recorded_email")


_ALL_TESTS = [
    test_email_none_is_a_noop,
    test_customer_key_none_is_a_noop,
    test_first_seen_pair_is_recorded_and_retrievable,
    test_repeat_webhook_same_pair_does_not_duplicate,
    test_one_email_can_map_to_multiple_customer_keys,
    test_list_all_customers_includes_recorded_email,
]


def main() -> int:
    failures = 0
    for test_fn in _ALL_TESTS:
        try:
            test_fn()
        except AssertionError as exc:
            print(f"FAIL  {test_fn.__name__}: {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001 -- surface a crash as a failure, not a traceback abort
            print(f"ERROR {test_fn.__name__}: {type(exc).__name__}: {exc}")
            failures += 1

    total = len(_ALL_TESTS)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
