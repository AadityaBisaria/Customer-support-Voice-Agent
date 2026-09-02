"""Value objects and state machines."""

import pytest

from store.domain import (
    ORDER_TRANSITIONS,
    REFUND_TRANSITIONS,
    RETURN_TRANSITIONS,
    IllegalTransition,
    Money,
    OrderStatus,
    PhoneNumber,
    RefundStatus,
    ReturnStatus,
    advance,
)


class TestMoney:
    def test_rupees_and_arithmetic(self):
        a = Money.rupees(1299)
        assert a.paise == 129900
        assert (a + Money.rupees(1)).paise == 130000
        assert (a - Money.rupees(299)).paise == 100000
        assert (a * 2).paise == 259800

    def test_floats_are_banned(self):
        with pytest.raises(TypeError):
            Money(12.5)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            Money.rupees(10) * 1.5  # type: ignore[operator]

    def test_speak(self):
        assert Money.rupees(1299).speak() == "1299 rupees"
        assert Money(129950).speak() == "1299 rupees 50 paise"


class TestPhoneNumber:
    @pytest.mark.parametrize(
        "raw",
        ["9876543210", "+91 98765 43210", "091-98765-43210".replace("091", "0"), "98765 43210"],
    )
    def test_parse_normalizes(self, raw: str):
        assert PhoneNumber.parse(raw).digits == "9876543210"

    def test_strips_country_code(self):
        assert PhoneNumber.parse("+919876543210").digits == "9876543210"
        assert PhoneNumber.parse("09876543210").digits == "9876543210"

    @pytest.mark.parametrize("raw", ["12345", "987654321012345", "", "abcdefghij"])
    def test_rejects_wrong_lengths(self, raw: str):
        with pytest.raises(ValueError):
            PhoneNumber.parse(raw)


class TestTransitions:
    def test_legal_order_transitions(self):
        assert advance(OrderStatus.PLACED, OrderStatus.CANCELLED, ORDER_TRANSITIONS)
        assert advance(OrderStatus.PLACED, OrderStatus.SHIPPED, ORDER_TRANSITIONS)
        assert advance(OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED, ORDER_TRANSITIONS)

    @pytest.mark.parametrize(
        "current, to",
        [
            (OrderStatus.DELIVERED, OrderStatus.CANCELLED),
            (OrderStatus.SHIPPED, OrderStatus.CANCELLED),
            (OrderStatus.CANCELLED, OrderStatus.PLACED),
        ],
    )
    def test_illegal_order_transitions(self, current, to):
        with pytest.raises(IllegalTransition):
            advance(current, to, ORDER_TRANSITIONS)

    def test_return_and_refund_machines(self):
        assert advance(ReturnStatus.REQUESTED, ReturnStatus.PICKUP_SCHEDULED, RETURN_TRANSITIONS)
        with pytest.raises(IllegalTransition):
            advance(ReturnStatus.COMPLETED, ReturnStatus.REQUESTED, RETURN_TRANSITIONS)
        assert advance(RefundStatus.INITIATED, RefundStatus.PROCESSED, REFUND_TRANSITIONS)
        with pytest.raises(IllegalTransition):
            advance(RefundStatus.INITIATED, RefundStatus.COMPLETED, REFUND_TRANSITIONS)
