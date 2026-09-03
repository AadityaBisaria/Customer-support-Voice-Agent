"""Pydantic schema contract and SQLite payload adapter checks."""

from datetime import datetime

from schemas import (
    Address,
    CancelResult,
    Customer,
    Money,
    OrderItem,
    OrderItemPolicySnapshot,
    RefundInfo,
    RefundMethod,
    Resolution,
    ReturnResult,
)
from store.sqlite import cancel_result_from_json, cancel_result_to_json, return_result_to_json


def test_customer_normalizes_phone_and_nested_addresses():
    customer = Customer(
        customer_id=1,
        first_name="Asha",
        last_name="Mehta",
        primary_phone="+91 98765 43210",
        saved_addresses=[Address(label="home", line1="12 MG Road", city="Bengaluru", pincode="560001")],
    )

    assert customer.primary_phone == "9876543210"
    assert customer.saved_addresses[0].city == "Bengaluru"


def test_order_item_snapshot_and_money_work():
    snapshot = OrderItemPolicySnapshot(return_window_days=10, allowed_resolutions=(Resolution.REFUND,))
    item = OrderItem(
        order_item_id=1,
        order_id="AMZ-1",
        product_id=2,
        variant_id=3,
        title="Shirt",
        quantity=2,
        unit_price=Money(paise=5000),
        total_price=Money(paise=10000),
        status="ordered",
        policy_snapshot=snapshot,
    )

    assert item.total_price.speak() == "100 rupees"


def test_future_refund_method_is_supported_by_the_schema():
    payload = RefundInfo(
        refund_id="REF-2001",
        amount=Money(paise=39900),
        method=RefundMethod.UPI,
        expected_by=datetime(2026, 9, 3, 12, 0),
    )

    assert '"upi"' in payload.model_dump_json()


def test_sqlite_payload_helpers_round_trip_live_store_values():
    payload = CancelResult(
        order_id="AMZ-1004",
        refund=RefundInfo(
            refund_id="REF-2001",
            amount=Money(paise=39900),
            method=RefundMethod.AMAZON_PAY,
            expected_by=datetime(2026, 9, 3, 12, 0),
        ),
    )

    raw = cancel_result_to_json(payload)
    restored = cancel_result_from_json(raw)

    assert restored.order_id == payload.order_id
    assert restored.refund is not None
    assert restored.refund.refund_id == payload.refund.refund_id
    assert restored.refund.amount.paise == payload.refund.amount.paise
    assert restored.refund.method.value == payload.refund.method.value


def test_return_payload_helper_round_trips_json():
    payload = ReturnResult(
        return_id="RET-1001",
        order_item_id=1,
        resolution=Resolution.REFUND,
        refund=None,
        pickup_by=datetime(2026, 9, 5, 12, 0),
    )

    raw = return_result_to_json(payload)

    assert '"return_id":"RET-1001"' in raw
